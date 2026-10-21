"""Typed views over instructions.json and the per-run results."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from grokbot_publisher.errors import ValidationError
from grokbot_publisher.util import iso, parse_iso

CARD_ID_RE = re.compile(r"^\d{8}-\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*$")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


def valid_card_id(card_id: str) -> bool:
    return bool(card_id) and len(card_id) <= 80 and bool(CARD_ID_RE.match(card_id))


@dataclass
class MediaItem:
    file: str
    type: str                      # "image" | "video"
    alt_text: str = ""
    path: Path | None = None       # resolved absolute path (set by the gate)

    @classmethod
    def parse(cls, raw: Any, index: int) -> MediaItem:
        if isinstance(raw, str):
            raw = {"file": raw}
        if not isinstance(raw, dict) or not raw.get("file"):
            raise ValidationError(f"media[{index}] must be an object with a 'file'")
        name = str(raw["file"])
        kind = str(raw.get("type") or "").lower()
        if not kind:
            ext = Path(name).suffix.lower()
            kind = "video" if ext in VIDEO_EXT else "image" if ext in IMAGE_EXT else ""
        if kind not in ("image", "video"):
            raise ValidationError(f"media[{index}] '{name}': type must be 'image' or 'video'")
        return cls(file=name, type=kind, alt_text=str(raw.get("alt_text") or ""))


@dataclass
class Post:
    index: int
    platform: str
    raw: dict

    @property
    def key(self) -> str:
        """Stable identity of this post inside its card (for idempotency)."""
        return f"{self.index}:{self.platform}"

    def get(self, name: str, default: Any = None) -> Any:
        return self.raw.get(name, default)

    @property
    def body(self) -> str:
        for name in ("caption", "text", "description"):
            if self.raw.get(name):
                return str(self.raw[name])
        return ""

    @property
    def hashtags(self) -> list[str]:
        tags = self.raw.get("hashtags") or []
        return [str(t) for t in tags] if isinstance(tags, list) else []

    @property
    def fmt(self) -> str:
        return str(self.raw.get("format") or "").lower()

    @property
    def privacy(self) -> str:
        return str(self.raw.get("privacy") or "").lower()

    def due_at(self) -> datetime | None:
        """None means 'now'."""
        value = self.raw.get("schedule", "now")
        if value in (None, "", "now"):
            return None
        try:
            return parse_iso(str(value))
        except ValueError as exc:
            raise ValidationError(f"posts[{self.index}] ({self.platform}): bad schedule: {exc}") from exc

    def media_names(self, card_media: list[MediaItem]) -> list[MediaItem]:
        names = self.raw.get("media")
        if names is None:
            return list(card_media)
        by_name = {m.file: m for m in card_media}
        picked = []
        for name in names:
            if name not in by_name:
                raise ValidationError(
                    f"posts[{self.index}] ({self.platform}) uses media '{name}' not listed in media[]"
                )
            picked.append(by_name[name])
        return picked


@dataclass
class Instructions:
    card_id: str
    card_version: int
    approved: Any
    approved_at: str
    media: list[MediaItem]
    posts: list[Post]
    content_sha256: str = ""
    ai_generated: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict) -> Instructions:
        if not isinstance(raw, dict):
            raise ValidationError("instructions.json must be a JSON object")
        version = raw.get("card_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValidationError("card_version must be a positive integer")
        posts_raw = raw.get("posts")
        if not isinstance(posts_raw, list) or not posts_raw:
            raise ValidationError("posts must be a non-empty list")
        posts = []
        for i, p in enumerate(posts_raw):
            if not isinstance(p, dict) or not p.get("platform"):
                raise ValidationError(f"posts[{i}] must be an object with a 'platform'")
            posts.append(Post(index=i, platform=str(p["platform"]).lower(), raw=p))
        media_raw = raw.get("media") or []
        if not isinstance(media_raw, list):
            raise ValidationError("media must be a list")
        return cls(
            card_id=str(raw.get("card_id") or ""),
            card_version=version,
            approved=raw.get("approved"),
            approved_at=str(raw.get("approved_at") or ""),
            media=[MediaItem.parse(m, i) for i, m in enumerate(media_raw)],
            posts=posts,
            content_sha256=str(raw.get("content_sha256") or ""),
            ai_generated=raw.get("ai_generated") is True,
            raw=raw,
        )


# Post result statuses. "published" and "already_published" count as done.
DONE = {"published", "already_published"}


@dataclass
class PostResult:
    post_index: int
    platform: str
    status: str                       # published | failed | rate_limited | scheduled | skipped |
                                      # already_published | dry_run | invalid | not_configured
    post_id: str = ""
    url: str = ""
    privacy: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)
    due_at: str = ""
    published_at: str = ""
    est_cost_usd: float | None = None
    extra: dict = field(default_factory=dict)  # data needed for takedown (urns, reply ids...)
    preview: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        return {k: v for k, v in data.items() if v not in ("", None, [], {})} | {
            "post_index": self.post_index,
            "platform": self.platform,
            "status": self.status,
        }


@dataclass
class CardReport:
    card_id: str
    card_version: int | None
    overall: str = "pending"          # success | partial | failed | refused | scheduled | dry_run
    started_at: str = field(default_factory=iso)
    finished_at: str = ""
    results: list[PostResult] = field(default_factory=list)
    refused_reasons: list[str] = field(default_factory=list)
    dry_run: bool = False

    def finish(self) -> CardReport:
        self.finished_at = iso()
        if self.refused_reasons:
            self.overall = "refused"
        elif self.dry_run:
            self.overall = "dry_run"
        else:
            attempted = [r for r in self.results if r.status not in ("scheduled", "skipped")]
            ok = [r for r in attempted if r.status in DONE]
            scheduled = any(r.status == "scheduled" for r in self.results)
            if attempted and len(ok) == len(attempted):
                self.overall = "scheduled" if scheduled else "success"
            elif ok:
                self.overall = "partial"
            elif attempted:
                self.overall = "failed"
            else:
                self.overall = "scheduled" if scheduled else "skipped"
        return self

    @property
    def complete(self) -> bool:
        """Every post on the card is done; Filing may move it to /published/."""
        return bool(self.results) and all(r.status in DONE for r in self.results)

    def summary_for_user(self) -> str:
        """One short, phone-friendly line per platform for the WhatsApp reply."""
        if self.refused_reasons:
            return f"Not posted ({self.card_id} v{self.card_version}): " + "; ".join(self.refused_reasons)
        icons = {"published": "✅", "already_published": "✅", "scheduled": "⏰", "dry_run": "\U0001f50d"}
        lines = []
        for r in self.results:
            icon = icons.get(r.status, "❌")
            if r.status in DONE:
                detail = r.url or r.post_id
                if r.privacy and r.privacy != "public":
                    detail += f" ({r.privacy})"
            elif r.status == "scheduled":
                detail = f"scheduled {r.due_at}"
            elif r.status == "dry_run":
                detail = "dry run, nothing sent"
            else:
                detail = (r.error or r.status)[:140]
            lines.append(f"{icon} {r.platform}: {detail}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "card_version": self.card_version,
            "overall": self.overall,
            "complete": self.complete,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "refused_reasons": self.refused_reasons,
            "summary_for_user": self.summary_for_user(),
            "results": [r.to_dict() for r in self.results],
        }
