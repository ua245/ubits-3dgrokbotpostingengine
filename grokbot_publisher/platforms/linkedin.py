"""LinkedIn via the versioned Posts API (/rest/posts).

* "as": "personal" (default) posts to the authorising member's profile and
  only needs the w_member_social scope.
* "as": "organization" posts as the ubits.tech page. That needs Community
  Management API approval and LINKEDIN_ORG_URN; until approved, it fails
  with a clear message instead of posting somewhere unexpected.
* Commentary uses LinkedIn's "little text" format: reserved characters are
  escaped (an unescaped "(" silently truncates the post) and #tags become
  hashtag templates.
* Member access tokens last 60 days. Programmatic refresh only exists for
  approved partners, so most apps re-authorise every two months.
"""

from __future__ import annotations

from urllib.parse import quote

import requests

from grokbot_publisher.errors import AuthExpiredError, PermanentError, RateLimitError, TransientError
from grokbot_publisher.http import body_text, json_body
from grokbot_publisher.media import MB, ImageSpec
from grokbot_publisher.models import MediaItem, Post, PostResult
from grokbot_publisher.platforms.base import STANDARD_1080, Platform, PreparedPost, require
from grokbot_publisher.text import compose, linkedin_escape, utf16_len
from grokbot_publisher.tokens import BearerAuth
from grokbot_publisher.util import iso

API = "https://api.linkedin.com/rest"
USERINFO = "https://api.linkedin.com/v2/userinfo"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
TEXT_LIMIT = 3000
IMAGE_SPEC = ImageSpec(name="linkedin-image", max_bytes=10 * MB, max_long=4096, formats=("JPEG", "PNG", "GIF"))


def classify(resp: requests.Response, platform: str):
    code = resp.status_code
    if 200 <= code < 300:
        return None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    msg = data.get("message") or body_text(resp)
    text = f"linkedin: HTTP {code}: {msg}"
    if code == 401:
        return AuthExpiredError(text, platform=platform)
    if code == 429:
        return RateLimitError(text, platform=platform)
    if code == 409 or code >= 500:
        return TransientError(text, platform=platform, detail={"status": code})
    if code == 403:
        return PermanentError(text + " (check the w_member_social / w_organization_social scope and page role)",
                              platform=platform)
    return PermanentError(text, platform=platform)


def _counter(text: str) -> int:
    return utf16_len(linkedin_escape(text))


class LinkedIn(Platform):
    name = "linkedin"

    @property
    def headers(self) -> dict:
        return {"LinkedIn-Version": self.s.linkedin_api_version, "X-Restli-Protocol-Version": "2.0.0"}

    # ------------------------------------------------------------ prepare
    def prepare(self, post: Post, media: list[MediaItem]) -> PreparedPost:
        images, videos = self.split_media(post, media)
        require(not (images and videos), "linkedin: a post can have images or one video, not both")
        require(len(videos) <= 1, "linkedin: at most one video")
        require(len(images) <= 20, "linkedin: at most 20 images")
        as_ = str(post.get("as") or "personal").lower()
        require(as_ in ("personal", "organization", "company"), "linkedin: 'as' must be personal or organization")
        if as_ != "personal":
            require(bool(self.s.linkedin_org_urn),
                    "linkedin: posting as the company page needs Community Management API approval "
                    "and LINKEDIN_ORG_URN; post with \"as\": \"personal\" until then")

        text = compose(post.body, post.hashtags, limit=TEXT_LIMIT, counter=_counter,
                       policy=self.s.caption_policy, label="text")
        prepared = []
        for item in media:
            if item.type == "image":
                prepared.append(self.ctx.media.prepare_image(item, IMAGE_SPEC, self.ctx.card_id))
            else:
                p = self.ctx.media.prepare_video(item, STANDARD_1080, self.ctx.card_id)
                self.check_duration(p, 3, 1800)
                prepared.append(p)
        visibility = str(post.get("visibility") or "PUBLIC").upper()
        return PreparedPost(
            post=post, fields={"text": text.text, "commentary": linkedin_escape(text.text),
                               "title": post.get("title") or ""},
            media=prepared, privacy="public" if visibility == "PUBLIC" else visibility.lower(),
            notes=text.notes + [n for p in prepared for n in p.notes],
            options={"as": "personal" if as_ == "personal" else "organization", "visibility": visibility},
        )

    # ------------------------------------------------------------ publish
    def auth(self) -> BearerAuth:
        return BearerAuth(self.ctx.tokens, self.name, refresher=self._refresh)

    def _refresh(self, entry: dict) -> dict | None:
        if not entry.get("refresh_token") or not self.s.linkedin_client_id:
            return None  # refresh tokens are only issued to approved partners
        resp = self.ctx.http.request(
            "POST", TOKEN_URL, platform=self.name, classify=classify, idempotent=True,
            data={"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                  "client_id": self.s.linkedin_client_id, "client_secret": self.s.linkedin_client_secret},
        )
        data = json_body(resp)
        return {k: data.get(k) for k in ("access_token", "refresh_token", "expires_in") if data.get(k)}

    def _call(self, method: str, url: str, auth: BearerAuth, **kw) -> requests.Response:
        headers = {**self.headers, **kw.pop("headers", {})}
        return self.ctx.http.request(method, url, platform=self.name, classify=classify, auth=auth,
                                     headers=headers, **kw)

    def _author(self, pp: PreparedPost, auth: BearerAuth) -> str:
        if pp.options["as"] == "organization":
            return self.s.linkedin_org_urn
        sub = auth.entry.get("user_id")
        if not sub:
            info = json_body(self.ctx.http.request("GET", USERINFO, platform=self.name, classify=classify,
                                                   auth=auth))
            sub = info.get("sub")
            if not sub:
                raise PermanentError("linkedin: couldn't read the member id (needs openid + profile scopes)")
            self.ctx.tokens.update(self.name, user_id=sub, username=info.get("name"))
        return sub if str(sub).startswith("urn:li:") else f"urn:li:person:{sub}"

    def publish(self, pp: PreparedPost) -> PostResult:
        auth = self.auth()
        author = self._author(pp, auth)
        urns = []
        for m in pp.media:
            urns.append(self._upload_image(m, author, auth) if m.kind == "image" else
                        self._upload_video(m, author, auth))

        body = {
            "author": author,
            "commentary": pp.fields["commentary"],
            "visibility": pp.options["visibility"],
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if len(urns) == 1:
            media = {"id": urns[0]}
            if pp.fields.get("title"):
                media["title"] = pp.fields["title"]
            alt = pp.media[0].source.alt_text if pp.media[0].source else ""
            if alt and pp.media[0].kind == "image":
                media["altText"] = alt
            body["content"] = {"media": media}
        elif urns:
            body["content"] = {"multiImage": {"images": [
                {"id": urn, **({"altText": m.source.alt_text} if m.source and m.source.alt_text else {})}
                for urn, m in zip(urns, pp.media, strict=True)
            ]}}

        resp = self._call("POST", f"{API}/posts", auth, idempotent=False, json=body)
        urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id") or json_body(resp).get("id", "")
        if not urn:
            raise PermanentError("linkedin: post created but no id returned", platform=self.name)
        return self.result(pp, status="published", post_id=urn, url=f"https://www.linkedin.com/feed/update/{urn}/",
                           published_at=iso(), extra={"author": author, "media": urns})

    def _upload_image(self, m, author: str, auth: BearerAuth) -> str:
        init = json_body(self._call("POST", f"{API}/images", auth, idempotent=True,
                                    params={"action": "initializeUpload"},
                                    json={"initializeUploadRequest": {"owner": author}}))
        value = init["value"]
        self.ctx.http.request("PUT", value["uploadUrl"], platform=self.name, classify=classify, auth=auth,
                              headers={"Content-Type": "application/octet-stream"},
                              body_factory=lambda: m.path.open("rb"))
        return value["image"]

    def _upload_video(self, m, author: str, auth: BearerAuth) -> str:
        init = json_body(self._call(
            "POST", f"{API}/videos", auth, idempotent=True, params={"action": "initializeUpload"},
            json={"initializeUploadRequest": {"owner": author, "fileSizeBytes": m.size,
                                              "uploadCaptions": False, "uploadThumbnail": False}},
        ))
        value = init["value"]
        etags = []
        with m.path.open("rb") as fh:
            for part in value["uploadInstructions"]:
                first, last = int(part["firstByte"]), int(part["lastByte"])
                fh.seek(first)
                chunk = fh.read(last - first + 1)
                resp = self.ctx.http.request("PUT", part["uploadUrl"], platform=self.name, classify=classify,
                                             headers={"Content-Type": "application/octet-stream"}, data=chunk)
                etags.append(resp.headers.get("ETag", "").strip('"'))
        video = value["video"]
        self._call("POST", f"{API}/videos", auth, idempotent=True, params={"action": "finalizeUpload"},
                   json={"finalizeUploadRequest": {"video": video, "uploadToken": value.get("uploadToken", ""),
                                                   "uploadedPartIds": etags}})

        def check():
            data = json_body(self._call("GET", f"{API}/videos/{quote(video, safe='')}", auth))
            status = data.get("status")
            if status == "PROCESSING_FAILED":
                raise PermanentError(f"linkedin: video processing failed: {data.get('processingFailureReason')}",
                                     platform=self.name)
            return status == "AVAILABLE", status

        self.poll(check, what=f"video {video}", first=5, cap=20)
        return video

    # ------------------------------------------------------------- delete
    def delete(self, entry: dict) -> str:
        urn = str(entry["post_id"])
        self._call("DELETE", f"{API}/posts/{quote(urn, safe='')}", self.auth(),
                   headers={"X-RestLi-Method": "DELETE"})
        return f"deleted linkedin post {urn}"

    def whoami(self) -> dict:
        info = json_body(self.ctx.http.request("GET", USERINFO, platform=self.name, classify=classify,
                                               auth=self.auth()))
        return {"account": info.get("name"), "id": info.get("sub")}
