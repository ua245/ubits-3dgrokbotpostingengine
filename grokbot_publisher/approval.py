"""Card lifecycle commands, so no agent ever hand-edits approval fields.

    new-card  render.mp4 -> /inbox/<card_id>/ (v1, not approved)
    approve   a person, in a terminal, approves one exact version
              -> sets approved + approved_version + content_sha256, moves to /approved/
    revise    any edit after approval: bump the version, clear the approval,
              move back to /inbox/ (the old YES no longer applies)

The gate still checks everything on every publish; these commands just make
the files consistent so the gate has nothing to argue about.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from grokbot_publisher.cabinet import CARD, INSTRUCTIONS, Cabinet
from grokbot_publisher.config import ALL_PLATFORMS
from grokbot_publisher.errors import ValidationError
from grokbot_publisher.gate import fingerprint
from grokbot_publisher.models import IMAGE_EXT, VIDEO_EXT, Instructions
from grokbot_publisher.util import atomic_write_json, iso, read_json, utcnow

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Sensible defaults for a vertical 3D short. Captions are left empty on purpose:
# `approve` refuses a card until every post has its text.
DEFAULT_POSTS = {
    "instagram": {"platform": "instagram", "format": "reel", "caption": "", "hashtags": []},
    "youtube": {"platform": "youtube", "format": "short", "title": "", "description": "", "hashtags": [],
                "privacy": "private"},
    "linkedin": {"platform": "linkedin", "as": "personal", "text": "", "hashtags": []},
    "x": {"platform": "x", "text": "", "hashtags": []},
    "tiktok": {"platform": "tiktok", "caption": "", "hashtags": [], "privacy": "private"},
}


def _load(card_dir: Path) -> tuple[dict, dict]:
    try:
        return read_json(card_dir / INSTRUCTIONS), read_json(card_dir / CARD)
    except FileNotFoundError as exc:
        raise ValidationError(f"{exc.filename} is missing") from None


def _history(card: dict, message: str) -> None:
    card.setdefault("history", []).append(f"{iso()} publisher: {message}")


def new_card(cabinet: Cabinet, slug: str, media: list[Path], *, platforms: tuple[str, ...] = ALL_PLATFORMS,
             brief: str = "", source: str = "blender_mcp", ai_generated: bool = True) -> Path:
    if not SLUG_RE.match(slug) or len(slug) > 60:
        raise ValidationError("slug must be lowercase words joined by hyphens, e.g. orbiting-cubes")
    unknown = set(platforms) - set(ALL_PLATFORMS)
    if unknown:
        raise ValidationError(f"unknown platform(s): {', '.join(sorted(unknown))}")
    if not media:
        raise ValidationError("give at least one media file")
    for path in media:
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXT | IMAGE_EXT:
            raise ValidationError(f"{path} is not an image or video file")

    card_id = f"{utcnow():%Y%m%d-%H%M}-{slug}"
    if cabinet.find(card_id):
        raise ValidationError(f"card {card_id} already exists")
    card_dir = cabinet.card_dir(card_id, "inbox")
    card_dir.mkdir(parents=True)
    items = []
    for path in media:
        shutil.copy2(path, card_dir / path.name)
        items.append({"file": path.name, "type": "video" if path.suffix.lower() in VIDEO_EXT else "image"})

    atomic_write_json(card_dir / INSTRUCTIONS, {
        "card_id": card_id,
        "card_version": 1,
        "approved": False,
        "approved_at": "",
        "ai_generated": ai_generated,
        "media": items,
        "posts": [dict(DEFAULT_POSTS[p], schedule="now") for p in platforms],
    })
    card = {
        "card_id": card_id,
        "card_version": 1,
        "brand": "ubits.tech",
        "input_source": source,
        "route": "own_upload",
        "user_brief": brief,
        "source": {"url": "", "original_creator": "", "media_type": items[0]["type"]},
        "target_platforms": list(platforms),
        "assets": [],
        "approval": {"status": "pending", "approved_version": None, "approved_at": "", "user_feedback": ""},
        "history": [],
    }
    _history(card, f"v1 created from {', '.join(p.name for p in media)}")
    atomic_write_json(card_dir / CARD, card)
    return card_dir


def unfinished_posts(raw: dict) -> list[str]:
    """Posts with no text yet: an empty caption is almost always unfinished work."""
    missing = []
    for i, post in enumerate(raw.get("posts") or []):
        fields = ("title",) if post.get("platform") == "youtube" else ("caption", "text", "description")
        if not any(str(post.get(f) or "").strip() for f in fields):
            missing.append(f"posts[{i}] ({post.get('platform')}) has no {'title' if fields == ('title',) else 'text'}")
    return missing


def approve(cabinet: Cabinet, card_id: str, version: int, *, via: str = "terminal",
            shown_sha256: str | None = None) -> Path:
    """`shown_sha256` is the fingerprint of what the person was shown; if the
    card changed while they were reading, the approval is refused."""
    found = cabinet.find(card_id)
    if not found or found[0] not in ("inbox", "approved"):
        raise ValidationError(f"card {card_id} isn't in /inbox/ or /approved/")
    folder, card_dir = found
    raw, card = _load(card_dir)
    Instructions.parse(raw)  # structural validation
    if raw.get("card_version") != version or card.get("card_version") != version:
        raise ValidationError(
            f"you're approving v{version}, but instructions.json is v{raw.get('card_version')} "
            f"and card.json is v{card.get('card_version')}"
        )
    missing = unfinished_posts(raw)
    if missing:
        raise ValidationError("not ready to approve: " + "; ".join(missing))

    digest = fingerprint(card_dir, raw)
    if shown_sha256 is not None and digest != shown_sha256:
        raise ValidationError("the card changed after you were shown it; nothing approved, run approve again")
    now = iso()
    raw.update(approved=True, approved_at=now, content_sha256=digest)
    card["approval"] = {**(card.get("approval") or {}), "status": "approved", "approved_version": version,
                        "approved_at": now, "approved_via": via}
    _history(card, f"v{version} approved via {via}")
    atomic_write_json(card_dir / INSTRUCTIONS, raw)
    atomic_write_json(card_dir / CARD, card)
    if folder == "inbox":
        card_dir = cabinet.move(card_id, "inbox", "approved")
    return card_dir


def revise(cabinet: Cabinet, card_id: str, reason: str = "") -> tuple[Path, int]:
    found = cabinet.find(card_id)
    if not found or found[0] not in ("inbox", "approved"):
        raise ValidationError(f"card {card_id} isn't in /inbox/ or /approved/")
    folder, card_dir = found
    raw, card = _load(card_dir)
    version = int(raw.get("card_version") or 1) + 1
    raw.update(card_version=version, approved=False, approved_at="")
    raw.pop("content_sha256", None)
    card["card_version"] = version
    card["approval"] = {**(card.get("approval") or {}), "status": "revise", "approved_version": None,
                        "approved_at": "", "user_feedback": reason}
    _history(card, f"v{version} opened for edits" + (f": {reason}" if reason else ""))
    atomic_write_json(card_dir / INSTRUCTIONS, raw)
    atomic_write_json(card_dir / CARD, card)
    if folder == "approved":
        card_dir = cabinet.move(card_id, "approved", "inbox")
    return card_dir, version
