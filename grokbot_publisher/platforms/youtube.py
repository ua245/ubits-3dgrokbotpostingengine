"""YouTube via the Data API v3 resumable upload (videos.insert).

* Unverified API projects can only upload as private until Google audits the
  app, so privacy defaults to "private".
* If your OAuth consent screen is in "Testing", Google expires refresh tokens
  after 7 days: re-run `auth url youtube` before the demo.
* Interrupted uploads resume from the byte the server confirms; nothing is
  uploaded twice and no duplicate video is created.
* format "short": vertical or square, up to 3 minutes. Landscape input is
  padded to 1080x1920 rather than cropped.
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
from grokbot_publisher.http import body_text, json_body
from grokbot_publisher.media import MB, VideoSpec
from grokbot_publisher.models import MediaItem, Post, PostResult
from grokbot_publisher.platforms.base import Platform, PreparedPost, require
from grokbot_publisher.text import compose, trim_to, utf8_len, youtube_safe
from grokbot_publisher.tokens import BearerAuth
from grokbot_publisher.util import iso

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHUNK = 32 * 256 * 1024  # 8 MiB; must be a multiple of 256 KiB
RATE_REASONS = {"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded", "uploadLimitExceeded",
                "dailyLimitExceeded"}
PRIVACY = {"private": "private", "unlisted": "unlisted", "public": "public"}

SHORTS_PAD = VideoSpec(name="shorts-1080x1920", max_long=1920, max_short=1080, max_bytes=2048 * MB,
                       min_fps=0, max_fps=60, pad_to=(1080, 1920))
ORIGINAL = VideoSpec(name="youtube-original", max_long=7680, max_short=4320, max_bytes=50 * 1024 * MB,
                     max_fps=120, require_h264=False, faststart=False)


def classify(resp: requests.Response, platform: str):
    code = resp.status_code
    if 200 <= code < 300 or code == 308:
        return None
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        err = {}
    reasons = {e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)}
    text = f"youtube: HTTP {code}: {err.get('message') or body_text(resp)}"
    if reasons & RATE_REASONS or code == 429:
        return RateLimitError(text + " (daily quota resets at midnight Pacific)", platform=platform)
    if code == 401 or "authError" in reasons:
        return AuthExpiredError(text, platform=platform)
    if code >= 500 or "backendError" in reasons:
        return TransientError(text, platform=platform, detail={"status": code})
    return PermanentError(text, platform=platform)


class YouTube(Platform):
    name = "youtube"

    # ------------------------------------------------------------ prepare
    def prepare(self, post: Post, media: list[MediaItem]) -> PreparedPost:
        images, videos = self.split_media(post, media)
        require(len(videos) == 1 and not images, "youtube: needs exactly one video")
        short = post.fmt == "short"
        privacy = PRIVACY.get(post.privacy or "private")
        require(privacy is not None, f"youtube: privacy must be private, unlisted or public, not {post.privacy!r}")

        notes: list[str] = []
        raw_title = post.get("title") or post.body.split("\n", 1)[0]
        require(raw_title.strip(), "youtube: needs a title")
        title = youtube_safe(raw_title.strip())
        if len(title) > 100:
            if self.s.caption_policy == "strict":
                raise ValidationError(f"youtube: title is {len(title)} chars; limit 100")
            title = trim_to(title, 100, len)
            notes.append("title: trimmed to 100 characters")
        desc = compose(youtube_safe(post.get("description") or post.body), post.hashtags, limit=5000,
                       counter=utf8_len, policy=self.s.caption_policy, label="description")
        notes += desc.notes
        tags, total = [], 0
        for tag in post.get("tags") or post.hashtags:
            tag = str(tag).lstrip("#").strip()
            if tag and total + len(tag) + 1 <= 500:
                tags.append(tag)
                total += len(tag) + 1

        source = videos[0]
        probe = self.ctx.media.prepare_video(source, ORIGINAL, self.ctx.card_id)
        if short:
            self.check_duration(probe, 1, 180)
            prepared = probe if probe.height >= probe.width else \
                self.ctx.media.prepare_video(source, SHORTS_PAD, self.ctx.card_id)
        else:
            prepared = probe
        notes += prepared.notes
        if privacy != "private":
            notes.append("youtube: unverified API projects still upload as private until Google's audit")
        return PreparedPost(
            post=post, fields={"title": title, "description": desc.text, "tags": tags}, media=[prepared],
            privacy=privacy, notes=notes,
            options={"short": short, "category_id": str(post.get("category_id") or self.s.youtube_category_id),
                     "made_for_kids": bool(post.get("made_for_kids", False))},
        )

    # ------------------------------------------------------------ publish
    def auth(self) -> BearerAuth:
        return BearerAuth(self.ctx.tokens, self.name, refresher=self._refresh)

    def _refresh(self, entry: dict) -> dict | None:
        if not entry.get("refresh_token") or not self.s.google_client_id:
            return None
        resp = self.ctx.http.request(
            "POST", TOKEN_URL, platform=self.name, classify=classify, idempotent=True,
            data={"client_id": self.s.google_client_id, "client_secret": self.s.google_client_secret,
                  "refresh_token": entry["refresh_token"], "grant_type": "refresh_token"},
        )
        data = json_body(resp)
        return {"access_token": data.get("access_token"), "expires_in": data.get("expires_in", 3600)}

    def publish(self, pp: PreparedPost) -> PostResult:
        auth = self.auth()
        video = pp.media[0]
        metadata = {
            "snippet": {"title": pp.fields["title"], "description": pp.fields["description"],
                        "tags": pp.fields["tags"], "categoryId": pp.options["category_id"]},
            "status": {"privacyStatus": pp.privacy, "selfDeclaredMadeForKids": pp.options["made_for_kids"],
                       "containsSyntheticMedia": self.ctx.ai_generated},
        }
        # Opening a session creates nothing yet, so it's safe to retry.
        init = self.ctx.http.request(
            "POST", UPLOAD_URL, platform=self.name, classify=classify, auth=auth, idempotent=True,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"X-Upload-Content-Length": str(video.size), "X-Upload-Content-Type": video.mime},
            json=metadata,
        )
        session_url = init.headers.get("Location")
        if not session_url:
            raise PermanentError("youtube: upload session was not created (no Location header)", platform=self.name)
        data = self._upload(session_url, video, auth)
        video_id = str(data["id"])
        actual = (data.get("status") or {}).get("privacyStatus", pp.privacy)
        url = f"https://youtube.com/shorts/{video_id}" if pp.options["short"] else f"https://www.youtube.com/watch?v={video_id}"
        result = self.result(pp, status="published", post_id=video_id, url=url, published_at=iso())
        result.privacy = actual
        return result

    def _upload(self, session_url: str, video, auth: BearerAuth) -> dict:
        size = video.size
        offset = 0
        failures = 0
        with video.path.open("rb") as fh:
            while True:
                fh.seek(offset)
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                try:
                    resp = self.ctx.http.request(
                        "PUT", session_url, platform=self.name, classify=classify, auth=auth, retries=0,
                        headers={"Content-Range": f"bytes {offset}-{end}/{size}", "Content-Type": video.mime},
                        data=chunk,
                    )
                except TransientError:
                    failures += 1
                    if failures > self.s.max_retries:
                        raise
                    self.ctx.sleep(self.ctx.http.backoff(failures - 1))
                    resp = self._status(session_url, size, auth)
                if resp.status_code in (200, 201):
                    return json_body(resp)
                new_offset = _next_offset(resp)
                if new_offset <= offset:
                    failures += 1  # no progress; don't loop forever
                    if failures > self.s.max_retries:
                        raise TransientError("youtube: upload is not making progress", platform=self.name)
                offset = new_offset
                if offset >= size:
                    # Server has every byte but hasn't answered with the video yet.
                    resp = self._status(session_url, size, auth)
                    if resp.status_code in (200, 201):
                        return json_body(resp)
                    raise TransientError("youtube: upload finished but no video was returned", platform=self.name)

    def _status(self, session_url: str, size: int, auth: BearerAuth) -> requests.Response:
        """Ask how many bytes the server has (resumable-upload protocol)."""
        return self.ctx.http.request(
            "PUT", session_url, platform=self.name, classify=classify, auth=auth,
            headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"}, data=b"",
        )

    # ------------------------------------------------------------- delete
    def delete(self, entry: dict) -> str:
        self.ctx.http.request("DELETE", f"{API}/videos", platform=self.name, classify=classify,
                              auth=self.auth(), params={"id": entry["post_id"]})
        return f"deleted youtube video {entry['post_id']}"

    def whoami(self) -> dict:
        resp = self.ctx.http.request("GET", f"{API}/channels", platform=self.name, classify=classify,
                                     auth=self.auth(), params={"part": "snippet", "mine": "true"})
        items = json_body(resp).get("items") or [{}]
        return {"account": (items[0].get("snippet") or {}).get("title"), "id": items[0].get("id")}


def _next_offset(resp: requests.Response) -> int:
    """308 Resume Incomplete carries 'Range: bytes=0-N' (absent: nothing stored)."""
    rng = resp.headers.get("Range", "")
    if rng.startswith("bytes=") and "-" in rng:
        return int(rng.split("-")[-1]) + 1
    return 0
