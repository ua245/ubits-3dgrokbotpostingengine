"""X via API v2: chunked media upload -> POST /2/tweets.

* Uses the v2 media endpoints (/2/media/upload/initialize, /{id}/append,
  /{id}/finalize). The old command=INIT style is no longer used.
* Pay-per-use: each created post is billed, and a post containing a link
  costs far more. Every result carries an estimated cost, and
  X_BLOCK_LINKS=true refuses link posts outright.
* X has no scheduling endpoint. Scheduling is done by the Publisher's
  watcher (posts with a future "schedule" wait in /approved/).
* OAuth 2.0 refresh tokens rotate: the new one is saved immediately.
"""

from __future__ import annotations

import requests

from grokbot_publisher.errors import (
    AuthExpiredError,
    PermanentError,
    RateLimitError,
    TransientError,
    ValidationError,
)
from grokbot_publisher.http import body_text, json_body, retry_after_seconds
from grokbot_publisher.media import MB, ImageSpec, VideoSpec
from grokbot_publisher.models import MediaItem, Post, PostResult
from grokbot_publisher.platforms.base import Platform, PreparedPost, require
from grokbot_publisher.text import compose, find_urls, x_weighted_len
from grokbot_publisher.tokens import BearerAuth
from grokbot_publisher.util import iso

API = "https://api.x.com/2"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
TEXT_LIMIT = 280
CHUNK = 4 * MB  # docs: keep segments at or below 5 MB
COST_POST = 0.015
COST_LINK_POST = 0.20

IMAGE_SPEC = ImageSpec(name="x-image", max_bytes=5 * MB, max_long=4096, formats=("JPEG", "PNG", "WEBP", "GIF"))
VIDEO_SPEC = VideoSpec(name="x-video", max_long=1280, max_short=1024, max_bytes=512 * MB,
                       min_fps=0, max_fps=40, target_fps=30, max_kbps=6000)


def classify(resp: requests.Response, platform: str):
    code = resp.status_code
    if 200 <= code < 300:
        return None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    detail = data.get("detail") or data.get("title") or "; ".join(
        e.get("message", "") for e in data.get("errors") or [] if isinstance(e, dict)
    ) or body_text(resp)
    text = f"x: HTTP {code}: {detail}"
    if code == 401:
        return AuthExpiredError(text, platform=platform)
    if code == 429:
        return RateLimitError(text, platform=platform, retry_after=retry_after_seconds(resp))
    if code == 402:
        return PermanentError(f"x: out of API credits (pay-per-use balance): {detail}", platform=platform)
    if code >= 500:
        return TransientError(text, platform=platform, detail={"status": code})
    return PermanentError(text, platform=platform)


class X(Platform):
    name = "x"

    # ------------------------------------------------------------ prepare
    def prepare(self, post: Post, media: list[MediaItem]) -> PreparedPost:
        images, videos = self.split_media(post, media)
        require(not (images and videos), "x: a post can have images or one video, not both")
        require(len(images) <= 4, "x: at most 4 images per post")
        require(len(videos) <= 1, "x: at most 1 video per post")
        require(post.body or media, "x: post needs text or media")

        main = compose(post.body, post.hashtags, limit=TEXT_LIMIT, counter=x_weighted_len,
                       separator="\n\n", policy=self.s.caption_policy, label="text")
        fields = {"text": main.text}
        notes = list(main.notes)
        reply_text = post.get("reply_text") or ""
        if reply_text:
            reply = compose(reply_text, (), limit=TEXT_LIMIT, counter=x_weighted_len,
                            policy=self.s.caption_policy, label="reply_text")
            fields["reply_text"] = reply.text
            notes += reply.notes

        cost = 0.0
        for key in ("text", "reply_text"):
            if key in fields:
                links = find_urls(fields[key])
                cost += COST_LINK_POST if links else COST_POST
                if links and self.s.x_block_links:
                    raise ValidationError(
                        f"x: {key} contains a link ({links[0]}) and X_BLOCK_LINKS is on "
                        f"(link posts cost ${COST_LINK_POST:.2f} vs ${COST_POST:.3f}). Put the link in the bio."
                    )
                if links:
                    notes.append(f"x: {key} has a link, billed at the link rate (~${COST_LINK_POST:.2f})")

        prepared = []
        for item in media:
            if item.type == "image":
                prepared.append(self.ctx.media.prepare_image(item, IMAGE_SPEC, self.ctx.card_id))
            else:
                p = self.ctx.media.prepare_video(item, VIDEO_SPEC, self.ctx.card_id)
                self.check_duration(p, 0.5, self.s.x_max_video_seconds)
                prepared.append(p)
        notes += [n for p in prepared for n in p.notes]
        return PreparedPost(post=post, fields=fields, media=prepared, privacy="public",
                            notes=notes, est_cost_usd=round(cost, 3))

    # ------------------------------------------------------------ publish
    def auth(self) -> BearerAuth:
        return BearerAuth(self.ctx.tokens, self.name, refresher=self._refresh)

    def _refresh(self, entry: dict) -> dict | None:
        if not entry.get("refresh_token") or not self.s.x_client_id:
            return None
        data = {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"]}
        basic = None
        if self.s.x_client_secret:  # confidential client: credentials go in Basic auth
            basic = (self.s.x_client_id, self.s.x_client_secret)
        else:                       # public client: client_id goes in the body
            data["client_id"] = self.s.x_client_id
        # Not retried once sent: refresh tokens are single-use.
        resp = self.ctx.http.request("POST", TOKEN_URL, platform=self.name, classify=classify,
                                     idempotent=False, basic_auth=basic, data=data)
        body = json_body(resp)
        return {k: body.get(k) for k in ("access_token", "refresh_token", "expires_in") if body.get(k)}

    def _call(self, method: str, path: str, auth: BearerAuth, **kw) -> dict:
        resp = self.ctx.http.request(method, f"{API}/{path}", platform=self.name, classify=classify,
                                     auth=auth, **kw)
        return json_body(resp)

    def publish(self, pp: PreparedPost) -> PostResult:
        auth = self.auth()
        media_ids = [self._upload(m, auth) for m in pp.media]
        body: dict = {"text": pp.fields["text"]}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        created = self._call("POST", "tweets", auth, idempotent=False, json=body)
        tweet_id = str(created["data"]["id"])
        username = auth.entry.get("username") or self.best_effort(lambda: self._username(auth), "", "username")
        url = f"https://x.com/{username}/status/{tweet_id}" if username else f"https://x.com/i/web/status/{tweet_id}"

        result = self.result(pp, status="published", post_id=tweet_id, url=url, published_at=iso(),
                             extra={"media_ids": media_ids})
        if pp.fields.get("reply_text"):
            reply = self.best_effort(
                lambda: self._call("POST", "tweets", auth, idempotent=False,
                                   json={"text": pp.fields["reply_text"],
                                         "reply": {"in_reply_to_tweet_id": tweet_id}}),
                None, "reply",
            )
            if reply:
                result.extra["reply_id"] = str(reply["data"]["id"])
            else:
                result.notes.append("x: main post is live but the reply failed; post the reply by hand")
        return result

    def _username(self, auth: BearerAuth) -> str:
        me = self._call("GET", "users/me", auth)
        name = (me.get("data") or {}).get("username", "")
        if name:
            self.ctx.tokens.update(self.name, username=name, user_id=(me.get("data") or {}).get("id"))
        return name

    def _upload(self, m, auth: BearerAuth) -> str:
        if m.kind == "video":
            category = "tweet_video"
        elif m.mime == "image/gif":
            category = "tweet_gif"
        else:
            category = "tweet_image"
        init = self._call("POST", "media/upload/initialize", auth, idempotent=True,
                          json={"media_type": m.mime, "total_bytes": m.size, "media_category": category})
        media_id = str(init["data"]["id"])
        with m.path.open("rb") as fh:
            index = 0
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                self._call("POST", f"media/upload/{media_id}/append", auth, idempotent=True,
                           data={"segment_index": str(index)}, files={"media": ("chunk", chunk)})
                index += 1
        final = self._call("POST", f"media/upload/{media_id}/finalize", auth, idempotent=True)
        info = (final.get("data") or {}).get("processing_info")
        if info and info.get("state") not in ("succeeded", None):
            self._wait_processing(media_id, auth, info)
        return media_id

    def _wait_processing(self, media_id: str, auth: BearerAuth, info: dict) -> None:
        state = {"info": info}

        def check():
            current = state["info"]
            if current.get("state") == "succeeded":
                return True, current
            if current.get("state") == "failed":
                err = current.get("error") or {}
                raise PermanentError(f"x: media processing failed: {err.get('message') or err}", platform=self.name)
            data = self._call("GET", "media/upload", auth, params={"command": "STATUS", "media_id": media_id})
            state["info"] = (data.get("data") or {}).get("processing_info") or {"state": "succeeded"}
            return state["info"].get("state") == "succeeded", state["info"]

        self.poll(check, what=f"media {media_id} processing",
                  first=float(info.get("check_after_secs") or 2), cap=10)

    # ------------------------------------------------------------- delete
    def delete(self, entry: dict) -> str:
        auth = self.auth()
        deleted = []
        for key in ("reply_id",):
            extra_id = (entry.get("extra") or {}).get(key)
            if extra_id:
                self._call("DELETE", f"tweets/{extra_id}", auth)
                deleted.append(extra_id)
        self._call("DELETE", f"tweets/{entry['post_id']}", auth)
        deleted.append(str(entry["post_id"]))
        return "deleted x post(s) " + ", ".join(deleted)

    def whoami(self) -> dict:
        auth = self.auth()
        me = self._call("GET", "users/me", auth)
        data = me.get("data") or {}
        return {"account": data.get("username"), "id": data.get("id")}
