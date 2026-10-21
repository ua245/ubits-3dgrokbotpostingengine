"""TikTok via the Content Posting API.

* Until TikTok audits the app, every post is private (SELF_ONLY). Privacy
  defaults to private, and the requested level must be one TikTok offers
  this account (creator_info), or the post is refused before uploading.
* Video defaults to FILE_UPLOAD (chunked PUT straight to TikTok).
  PULL_FROM_URL only works from a domain or URL prefix you've verified in the
  TikTok developer portal, so a random R2/S3 presigned link will be rejected.
  Photo posts only support PULL_FROM_URL.
* "mode": "draft" sends the video to the creator's TikTok inbox to finish in
  the app (needs the video.upload scope) instead of posting it directly.
* There is no delete endpoint: takedown is manual, in the TikTok app.
"""

from __future__ import annotations

import requests

from grokbot_publisher.errors import (
    AuthExpiredError,
    ManualActionRequired,
    PermanentError,
    RateLimitError,
    TransientError,
    ValidationError,
)
from grokbot_publisher.http import body_text, json_body
from grokbot_publisher.media import MB, ImageSpec
from grokbot_publisher.models import MediaItem, Post, PostResult
from grokbot_publisher.platforms.base import STANDARD_1080, Platform, PreparedPost, require
from grokbot_publisher.text import compose, utf16_len
from grokbot_publisher.tokens import BearerAuth
from grokbot_publisher.util import iso

API = "https://open.tiktokapis.com/v2"
TOKEN_URL = f"{API}/oauth/token/"
CHUNK = 10 * MB          # each chunk 5-64 MB; the last one absorbs the remainder
SINGLE_MAX = 64 * MB     # up to this size, upload in one piece
PRIVACY = {
    "private": "SELF_ONLY", "self_only": "SELF_ONLY",
    "public": "PUBLIC_TO_EVERYONE", "friends": "MUTUAL_FOLLOW_FRIENDS", "followers": "FOLLOWER_OF_CREATOR",
}
RATE_CODES = {"rate_limit_exceeded", "spam_risk_too_many_posts", "spam_risk_too_many_pending_share",
              "reached_active_user_cap"}
AUTH_CODES = {"access_token_invalid", "access_token_expired"}
HINTS = {
    "unaudited_client_can_only_post_to_private_accounts":
        "app isn't audited yet: set privacy to private (SELF_ONLY) or finish TikTok's audit",
    "url_ownership_unverified":
        "PULL_FROM_URL needs the media domain verified in the TikTok developer portal; use FILE_UPLOAD",
    "privacy_level_option_mismatch": "requested privacy isn't allowed for this account",
}
PHOTO_SPEC = ImageSpec(name="tiktok-photo", max_bytes=20 * MB, max_long=1920, formats=("JPEG", "WEBP"))


def classify(resp: requests.Response, platform: str):
    code = resp.status_code
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    api_code = err.get("code") or ""
    if 200 <= code < 300 and api_code in ("", "ok"):
        return None
    msg = err.get("message") or body_text(resp)
    hint = HINTS.get(api_code)
    text = f"tiktok: {api_code or f'HTTP {code}'}: {msg}" + (f" ({hint})" if hint else "")
    if api_code in RATE_CODES or code == 429:
        return RateLimitError(text, platform=platform)
    if api_code in AUTH_CODES or code == 401:
        return AuthExpiredError(text, platform=platform)
    if code >= 500 or api_code == "internal_error":
        return TransientError(text, platform=platform, detail={"status": code})
    return PermanentError(text, platform=platform)


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules."""
    if size <= SINGLE_MAX:
        return size, 1
    count = size // CHUNK
    return CHUNK, count


class TikTok(Platform):
    name = "tiktok"

    # ------------------------------------------------------------ prepare
    def prepare(self, post: Post, media: list[MediaItem]) -> PreparedPost:
        images, videos = self.split_media(post, media)
        privacy_key = post.privacy or "private"
        require(privacy_key in PRIVACY, f"tiktok: privacy must be one of {', '.join(PRIVACY)}")
        mode = str(post.get("mode") or "direct").lower()
        require(mode in ("direct", "draft"), "tiktok: mode must be direct or draft")
        notes: list[str] = []

        if videos:
            require(len(videos) == 1 and not images, "tiktok: a video post needs exactly one video")
            cap = compose(post.body, post.hashtags, limit=2200, counter=utf16_len,
                          policy=self.s.caption_policy, label="caption")
            p = self.ctx.media.prepare_video(videos[0], STANDARD_1080, self.ctx.card_id)
            self.check_duration(p, 3, 600)
            prepared, kind, fields = [p], "video", {"title": cap.text}
        else:
            require(1 <= len(images) <= 35, "tiktok: needs one video or 1-35 photos")
            if self.s.tiktok_source != "PULL_FROM_URL":
                raise ValidationError("tiktok: photo posts need TIKTOK_SOURCE=PULL_FROM_URL and a verified domain")
            require(mode == "direct", "tiktok: draft mode is for videos only")
            title = compose(post.get("title") or post.body.split("\n", 1)[0], (), limit=90,
                            counter=utf16_len, policy=self.s.caption_policy, label="title")
            cap = compose(post.body, post.hashtags, limit=4000, counter=utf16_len,
                          policy=self.s.caption_policy, label="description")
            notes += title.notes
            prepared = [self.ctx.media.prepare_image(i, PHOTO_SPEC, self.ctx.card_id) for i in images]
            kind, fields = "photo", {"title": title.text, "description": cap.text}
        notes += cap.notes + [n for p in prepared for n in p.notes]
        privacy = PRIVACY[privacy_key]
        return PreparedPost(
            post=post, fields=fields, media=prepared,
            privacy="private" if privacy == "SELF_ONLY" else privacy_key, notes=notes,
            options={"kind": kind, "mode": mode, "privacy_level": privacy,
                     "disable_comment": bool(post.get("disable_comment", False)),
                     "disable_duet": bool(post.get("disable_duet", False)),
                     "disable_stitch": bool(post.get("disable_stitch", False)),
                     "brand_organic": bool(post.get("brand_organic", False)),
                     "brand_content": bool(post.get("brand_content", False))},
        )

    # ------------------------------------------------------------ publish
    def auth(self) -> BearerAuth:
        return BearerAuth(self.ctx.tokens, self.name, refresher=self._refresh)

    def _refresh(self, entry: dict) -> dict | None:
        if not entry.get("refresh_token") or not self.s.tiktok_client_key:
            return None
        resp = self.ctx.http.request(
            "POST", TOKEN_URL, platform=self.name, classify=classify, idempotent=False,
            data={"client_key": self.s.tiktok_client_key, "client_secret": self.s.tiktok_client_secret,
                  "grant_type": "refresh_token", "refresh_token": entry["refresh_token"]},
        )
        data = json_body(resp)
        return {k: data.get(k) for k in ("access_token", "refresh_token", "expires_in", "refresh_expires_in")
                if data.get(k)}

    def _post(self, path: str, auth: BearerAuth, body: dict, idempotent: bool) -> dict:
        resp = self.ctx.http.request(
            "POST", f"{API}/{path}", platform=self.name, classify=classify, auth=auth, idempotent=idempotent,
            headers={"Content-Type": "application/json; charset=UTF-8"}, json=body,
        )
        return json_body(resp).get("data") or {}

    def creator_info(self, auth: BearerAuth) -> dict:
        return self._post("post/publish/creator_info/query/", auth, {}, idempotent=True)

    def publish(self, pp: PreparedPost) -> PostResult:
        auth = self.auth()
        opts = pp.options
        info = self.creator_info(auth)
        username = info.get("creator_username") or auth.entry.get("username", "")
        if username and username != auth.entry.get("username"):
            self.ctx.tokens.update(self.name, username=username)

        if opts["mode"] == "direct":
            allowed = info.get("privacy_level_options") or []
            if allowed and opts["privacy_level"] not in allowed:
                raise PermanentError(
                    f"tiktok: privacy {opts['privacy_level']} isn't available for this account "
                    f"(allowed: {', '.join(allowed)}). Unaudited apps can only post SELF_ONLY.",
                    platform=self.name,
                )
            max_dur = info.get("max_video_post_duration_sec")
            if opts["kind"] == "video" and max_dur and pp.media[0].duration > float(max_dur):
                raise ValidationError(f"tiktok: video is {pp.media[0].duration:.0f}s; this account allows {max_dur}s")

        self._stored = []
        try:
            if opts["kind"] == "photo":
                publish_id = self._publish_photos(pp, auth)
            else:
                publish_id = self._publish_video(pp, auth)
            status = self._wait(publish_id, auth, draft=opts["mode"] == "draft")
        finally:
            for obj in self._stored:  # TikTok has pulled (or given up on) the files by now
                self.ctx.storage.delete(obj)

        ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
        post_id = str(ids[0]) if ids else publish_id
        profile = f"https://www.tiktok.com/@{username}" if username else "https://www.tiktok.com/"
        url = f"{profile}/video/{post_id}" if ids and username else profile
        result = self.result(pp, status="published", post_id=post_id, url=url, published_at=iso(),
                             extra={"publish_id": publish_id})
        if opts["mode"] == "draft":
            result.privacy = "draft"
            result.notes.append("tiktok: sent to the TikTok inbox; finish posting in the app")
        elif not ids:
            result.notes.append("tiktok: private post, only visible to the account owner in the app")
        return result

    def _post_info(self, pp: PreparedPost) -> dict:
        o = pp.options
        info = {"privacy_level": o["privacy_level"], "disable_comment": o["disable_comment"],
                "brand_content_toggle": o["brand_content"], "brand_organic_toggle": o["brand_organic"]}
        info.update(pp.fields)
        if o["kind"] == "video":
            info.update(disable_duet=o["disable_duet"], disable_stitch=o["disable_stitch"],
                        is_aigc=self.ctx.ai_generated)
        return info

    def _publish_video(self, pp: PreparedPost, auth: BearerAuth) -> str:
        video = pp.media[0]
        draft = pp.options["mode"] == "draft"
        path = "post/publish/inbox/video/init/" if draft else "post/publish/video/init/"
        if self.s.tiktok_source == "PULL_FROM_URL":
            obj = self.ctx.storage.put(video.path, self.ctx.card_id)
            self._stored.append(obj)
            source = {"source": "PULL_FROM_URL", "video_url": obj.url}
        else:
            chunk_size, count = chunk_plan(video.size)
            source = {"source": "FILE_UPLOAD", "video_size": video.size, "chunk_size": chunk_size,
                      "total_chunk_count": count}
        body = {"source_info": source} if draft else {"post_info": self._post_info(pp), "source_info": source}
        data = self._post(path, auth, body, idempotent=False)
        if source["source"] == "FILE_UPLOAD":
            self._upload_chunks(data["upload_url"], video)
        return str(data["publish_id"])

    def _publish_photos(self, pp: PreparedPost, auth: BearerAuth) -> str:
        for m in pp.media:
            self._stored.append(self.ctx.storage.put(m.path, self.ctx.card_id))
        urls = [obj.url for obj in self._stored]
        body = {
            "post_info": self._post_info(pp),
            "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": urls},
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        }
        data = self._post("post/publish/content/init/", auth, body, idempotent=False)
        return str(data["publish_id"])

    def _upload_chunks(self, upload_url: str, video) -> None:
        size = video.size
        chunk_size, count = chunk_plan(size)
        with video.path.open("rb") as fh:
            for i in range(count):
                start = i * chunk_size
                end = size - 1 if i == count - 1 else start + chunk_size - 1
                fh.seek(start)
                chunk = fh.read(end - start + 1)
                self.ctx.http.request(
                    "PUT", upload_url, platform=self.name, classify=classify,
                    headers={"Content-Type": video.mime, "Content-Range": f"bytes {start}-{end}/{size}"},
                    data=chunk,
                )

    def _wait(self, publish_id: str, auth: BearerAuth, draft: bool) -> dict:
        done_states = {"SEND_TO_USER_INBOX"} if draft else {"PUBLISH_COMPLETE"}

        def check():
            data = self._post("post/publish/status/fetch/", auth, {"publish_id": publish_id}, idempotent=True)
            status = data.get("status")
            if status == "FAILED":
                raise PermanentError(f"tiktok: publish failed: {data.get('fail_reason', 'unknown')}",
                                     platform=self.name)
            # Private posts can stop at SEND_TO_USER_INBOX/PUBLISH_COMPLETE without a public id.
            return status in done_states or status == "PUBLISH_COMPLETE", data

        return self.poll(check, what=f"publish {publish_id}", first=5, cap=15)

    # ------------------------------------------------------------- delete
    def delete(self, entry: dict) -> str:
        raise ManualActionRequired(
            "tiktok: the Content Posting API has no delete endpoint; delete it in the TikTok app "
            + (entry.get("url") or ""),
            platform=self.name,
        )

    def whoami(self) -> dict:
        info = self.creator_info(self.auth())
        return {"account": info.get("creator_username"), "privacy_options": info.get("privacy_level_options")}
