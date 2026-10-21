"""What every platform module looks like.

Each platform does two things, always in this order:

    prepare(post, media) -> PreparedPost     # validate + text + media, no network
    publish(prepared)    -> PostResult       # official API calls only

`prepare` is also what dry runs show, so the preview matches what gets posted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from grokbot_publisher.config import Settings
from grokbot_publisher.errors import ManualActionRequired, PermanentError, TransientError, ValidationError
from grokbot_publisher.http import Http
from grokbot_publisher.media import MB, MediaTool, Prepared, VideoSpec
from grokbot_publisher.models import MediaItem, Post, PostResult
from grokbot_publisher.tokens import TokenStore

# One shared 1080p H.264/AAC spec for Instagram Reels, TikTok and LinkedIn, so a
# single encode (cached) serves all three.
STANDARD_1080 = VideoSpec(
    name="h264-1080", max_long=1920, max_short=1080, max_bytes=300 * MB,
    min_fps=23, max_fps=60, target_fps=30, max_kbps=10000,
)


@dataclass
class Context:
    settings: Settings
    http: Http
    tokens: TokenStore
    media: MediaTool
    storage_factory: Callable[[], object]
    card_id: str
    ai_generated: bool = False
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    _storage: object = None

    @property
    def storage(self):
        if self._storage is None:
            self._storage = self.storage_factory()
        return self._storage


@dataclass
class PreparedPost:
    post: Post
    fields: dict
    media: list[Prepared]
    privacy: str
    notes: list[str] = field(default_factory=list)
    est_cost_usd: float | None = None
    options: dict = field(default_factory=dict)

    def preview(self) -> dict:
        return {
            "fields": self.fields,
            "media": [
                {"file": m.source.file if m.source else m.path.name, "prepared": str(m.path), "kind": m.kind,
                 "size_mb": round(m.size / MB, 2), "dimensions": f"{m.width}x{m.height}",
                 **({"duration_s": round(m.duration, 1)} if m.kind == "video" else {})}
                for m in self.media
            ],
            "privacy": self.privacy,
            **({"options": self.options} if self.options else {}),
        }


class Platform:
    name = ""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.s = ctx.settings
        self.log = logging.getLogger(f"grokbot_publisher.{self.name}")

    # -- to implement -----------------------------------------------------
    def prepare(self, post: Post, media: list[MediaItem]) -> PreparedPost:
        raise NotImplementedError

    def publish(self, pp: PreparedPost) -> PostResult:
        raise NotImplementedError

    def delete(self, entry: dict) -> str:
        raise ManualActionRequired(f"{self.name}: delete isn't supported; remove it in the app")

    def whoami(self) -> dict:
        raise NotImplementedError

    # -- shared helpers ---------------------------------------------------
    def result(self, pp: PreparedPost, **kw) -> PostResult:
        return PostResult(
            post_index=pp.post.index, platform=self.name, privacy=pp.privacy,
            notes=list(pp.notes), est_cost_usd=pp.est_cost_usd, **kw,
        )

    @staticmethod
    def split_media(post: Post, media: list[MediaItem]) -> tuple[list[MediaItem], list[MediaItem]]:
        images = [m for m in media if m.type == "image"]
        videos = [m for m in media if m.type == "video"]
        return images, videos

    def check_duration(self, prepared: Prepared, low: float, high: float) -> None:
        if prepared.duration and not (low <= prepared.duration <= high):
            raise ValidationError(
                f"{self.name}: {prepared.source.file if prepared.source else 'video'} is "
                f"{prepared.duration:.1f}s; allowed {low:g}-{high:g}s (videos are never cut automatically)"
            )

    def poll(self, check: Callable[[], tuple[bool, object]], *, what: str,
             timeout: float | None = None, first: float = 3.0, cap: float = 15.0):
        """Call `check` until it reports done, waiting longer between calls."""
        deadline = self.ctx.clock() + (timeout or self.s.poll_timeout)
        delay = first
        while True:
            done, value = check()
            if done:
                return value
            if self.ctx.clock() + delay > deadline:
                raise TransientError(f"{self.name}: timed out waiting for {what}", platform=self.name)
            self.ctx.sleep(delay)
            delay = min(cap, delay * 1.5)

    def best_effort(self, fn: Callable, default=None, what: str = "lookup"):
        """For calls *after* a post is live: a failure here must never turn a
        successful post into a reported failure (and a re-post)."""
        try:
            return fn()
        except Exception as exc:
            self.log.warning("%s: %s failed after posting (post is live): %s", self.name, what, exc)
            return default


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def permanent(platform: str, message: str) -> PermanentError:
    return PermanentError(f"{platform}: {message}", platform=platform)
