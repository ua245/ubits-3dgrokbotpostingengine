"""Instagram via the Instagram Graph API: create container -> wait -> publish.

* Media must be at a public URL, so files go to temporary storage first and
  are deleted again as soon as Instagram has fetched them.
* Containers are created and published in the same run (they expire after
  24h), never prepared in advance.
* The publishing quota is checked *before* creating anything.
* Works with "Instagram API with Instagram Login" (graph.instagram.com, the
  default, no Facebook Page needed) or with Facebook Login
  (INSTAGRAM_API_HOST=graph.facebook.com). Only the Facebook Login flavour
  can delete posts through the API.
"""

from __future__ import annotations

import requests

from grokbot_publisher.errors import (
    AuthExpiredError,
    ManualActionRequired,
    PermanentError,
    RateLimitError,
    TransientError,
)
from grokbot_publisher.http import default_classify, json_body
from grokbot_publisher.media import MB, ImageSpec
from grokbot_publisher.models import MediaItem, Post, PostResult
from grokbot_publisher.platforms.base import STANDARD_1080, Platform, PreparedPost, require
from grokbot_publisher.text import compose, utf16_len
from grokbot_publisher.tokens import BearerAuth
from grokbot_publisher.util import iso

IMAGE_SPEC = ImageSpec(
    name="instagram-image", max_bytes=8 * MB, max_long=1440, min_aspect=0.8, max_aspect=1.91, formats=("JPEG",)
)
CAPTION_LIMIT = 2200
MAX_HASHTAGS = 30
RATE_CODES = {4, 17, 32, 613}
PUBLISH_LIMIT_SUBCODE = 2207042
NOT_READY_SUBCODE = 2207027


def classify(resp: requests.Response, platform: str):
    if resp.ok:
        return None
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return default_classify(resp, platform)
    code, sub = err.get("code"), err.get("error_subcode")
    msg = err.get("error_user_msg") or err.get("message") or resp.reason
    text = f"instagram: {msg} (code {code}{f'/{sub}' if sub else ''})"
    if code == 190 or resp.status_code == 401:
        return AuthExpiredError(text, platform=platform)
    if code in RATE_CODES or sub == PUBLISH_LIMIT_SUBCODE:
        return RateLimitError(text, platform=platform)
    if sub == NOT_READY_SUBCODE:
        return TransientError(text, platform=platform, detail={"safe_retry": True})
    if err.get("is_transient") or code in (1, 2) or resp.status_code >= 500:
        return TransientError(text, platform=platform, detail={"status": resp.status_code})
    return PermanentError(text, platform=platform)


class Instagram(Platform):
    name = "instagram"

    @property
    def base(self) -> str:
        return f"https://{self.s.instagram_api_host}/{self.s.instagram_api_version}"

    @property
    def instagram_login(self) -> bool:
        return "graph.instagram.com" in self.s.instagram_api_host

    # ------------------------------------------------------------ prepare
    def prepare(self, post: Post, media: list[MediaItem]) -> PreparedPost:
        images, videos = self.split_media(post, media)
        fmt = post.fmt or ("reel" if videos and len(media) == 1 else "carousel" if len(media) > 1 else "image")
        require(fmt in ("image", "reel", "carousel"), f"instagram: format must be image, reel or carousel, not {fmt!r}")
        if fmt == "image":
            require(len(images) == 1 and not videos, "instagram: an image post needs exactly one image")
        elif fmt == "reel":
            require(len(videos) == 1 and not images, "instagram: a reel needs exactly one video")
        else:
            require(2 <= len(media) <= 20, "instagram: a carousel needs 2-20 images/videos")

        cap = compose(post.body, post.hashtags, limit=CAPTION_LIMIT, counter=utf16_len,
                      max_tags=MAX_HASHTAGS, policy=self.s.caption_policy)
        prepared = []
        for item in media:
            if item.type == "image":
                prepared.append(self.ctx.media.prepare_image(item, IMAGE_SPEC, self.ctx.card_id))
            else:
                p = self.ctx.media.prepare_video(item, STANDARD_1080, self.ctx.card_id)
                self.check_duration(p, 3, 900 if fmt == "reel" else 60)
                prepared.append(p)
        notes = cap.notes + [n for p in prepared for n in p.notes]
        return PreparedPost(
            post=post, fields={"caption": cap.text}, media=prepared, privacy="public", notes=notes,
            options={"format": fmt, "share_to_feed": post.get("share_to_feed", True)},
        )

    # ------------------------------------------------------------ publish
    def auth(self) -> BearerAuth:
        refresher = self._refresh if self.instagram_login else None
        # Long-lived tokens last 60 days; refresh during the final week.
        return BearerAuth(self.ctx.tokens, self.name, refresher=refresher, early=7 * 86400, param="access_token")

    def _refresh(self, entry: dict) -> dict | None:
        # Note: Instagram only refreshes tokens that are at least 24h old and still valid.
        resp = self.ctx.http.request(
            "GET", "https://graph.instagram.com/refresh_access_token", platform=self.name, classify=classify,
            params={"grant_type": "ig_refresh_token", "access_token": entry["access_token"]},
        )
        data = json_body(resp)
        return {"access_token": data.get("access_token"), "expires_in": data.get("expires_in", 5184000)}

    def _call(self, method: str, path: str, auth: BearerAuth, **kw) -> dict:
        resp = self.ctx.http.request(method, f"{self.base}/{path}", platform=self.name, classify=classify,
                                     auth=auth, **kw)
        return json_body(resp)

    def _user_id(self, auth: BearerAuth) -> str:
        if auth.entry.get("user_id"):
            return str(auth.entry["user_id"])
        if not self.instagram_login:
            raise PermanentError("instagram: set user_id (the IG business account id) for Facebook Login tokens")
        me = self._call("GET", "me", auth, params={"fields": "user_id,username"})
        uid = str(me.get("user_id") or me.get("id"))
        self.ctx.tokens.update(self.name, user_id=uid, username=me.get("username"))
        return uid

    def check_quota(self, auth: BearerAuth, user_id: str) -> dict:
        data = self._call("GET", f"{user_id}/content_publishing_limit", auth,
                          params={"fields": "quota_usage,config"})
        row = (data.get("data") or [{}])[0]
        usage = int(row.get("quota_usage") or 0)
        total = int((row.get("config") or {}).get("quota_total") or 0)
        if total and usage >= total:
            raise RateLimitError(f"instagram: publishing limit reached ({usage}/{total} in 24h)", platform=self.name)
        return {"quota_usage": usage, "quota_total": total}

    def publish(self, pp: PreparedPost) -> PostResult:
        auth = self.auth()
        user_id = self._user_id(auth)
        quota = self.check_quota(auth, user_id)
        fmt = pp.options["format"]
        storage = self.ctx.storage
        stored = []
        try:
            urls = []
            for m in pp.media:
                obj = storage.put(m.path, self.ctx.card_id)
                stored.append(obj)
                urls.append(obj.url)

            if fmt == "carousel":
                children = []
                for m, url in zip(pp.media, urls, strict=True):
                    params = {"is_carousel_item": "true"}
                    if m.kind == "image":
                        params["image_url"] = url
                    else:
                        params.update(media_type="VIDEO", video_url=url)
                    children.append(self._create(user_id, auth, params))
                for child in children:
                    self._wait(child, auth)
                params = {"media_type": "CAROUSEL", "children": ",".join(children), "caption": pp.fields["caption"]}
            elif fmt == "reel":
                params = {"media_type": "REELS", "video_url": urls[0], "caption": pp.fields["caption"],
                          "share_to_feed": "true" if pp.options.get("share_to_feed", True) else "false"}
            else:
                params = {"image_url": urls[0], "caption": pp.fields["caption"]}
                alt = pp.media[0].source.alt_text if pp.media[0].source else ""
                if alt:
                    params["alt_text"] = alt
            if self.ctx.ai_generated:
                params["is_ai_generated"] = "true"

            container = self._create(user_id, auth, params)
            self._wait(container, auth)
            published = self._call("POST", f"{user_id}/media_publish", auth, idempotent=False,
                                   params={"creation_id": container})
            media_id = str(published["id"])
        finally:
            for obj in stored:
                storage.delete(obj)

        info = self.best_effort(
            lambda: self._call("GET", media_id, auth, params={"fields": "permalink,shortcode"}), {}, "permalink"
        )
        result = self.result(pp, status="published", post_id=media_id, url=info.get("permalink", ""),
                             published_at=iso(), extra={"container_id": container, "user_id": user_id})
        if quota.get("quota_total"):
            result.notes.append(f"instagram quota: {quota['quota_usage'] + 1}/{quota['quota_total']} in 24h")
        return result

    def _create(self, user_id: str, auth: BearerAuth, params: dict) -> str:
        # Creating a container publishes nothing, so it's safe to retry.
        data = self._call("POST", f"{user_id}/media", auth, idempotent=True, data=params)
        return str(data["id"])

    def _wait(self, container: str, auth: BearerAuth) -> None:
        def check():
            data = self._call("GET", container, auth, params={"fields": "status_code,status"})
            code = data.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return True, data
            if code in ("ERROR", "EXPIRED"):
                raise PermanentError(f"instagram: media processing {code}: {data.get('status', '')}",
                                     platform=self.name)
            return False, data

        self.poll(check, what=f"container {container}", first=3, cap=15)

    # ------------------------------------------------------------- delete
    def delete(self, entry: dict) -> str:
        if self.instagram_login:
            raise ManualActionRequired(
                "instagram: the Instagram Login API can't delete posts. Delete it in the app: "
                + (entry.get("url") or entry.get("post_id", "")),
                platform=self.name,
            )
        self._call("DELETE", str(entry["post_id"]), self.auth())
        return f"deleted instagram media {entry['post_id']}"

    def whoami(self) -> dict:
        auth = self.auth()
        if self.instagram_login:
            me = self._call("GET", "me", auth, params={"fields": "user_id,username"})
            return {"account": me.get("username"), "id": me.get("user_id")}
        uid = self._user_id(auth)
        me = self._call("GET", uid, auth, params={"fields": "username"})
        return {"account": me.get("username"), "id": uid}
