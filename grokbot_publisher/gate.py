"""The safety gate. Runs before anything is prepared, uploaded or posted.

Every check is a plain comparison of values on disk. No check can be skipped
by a flag, and no AI output can talk its way past one. If any check fails the
whole card is refused and nothing goes anywhere.

Checks, in order:
 1. card_id is well-formed and the card sits in /approved/ (not inbox etc.)
 2. instructions.json parses; "approved" is the JSON literal true
 3. instructions.card_id matches the folder
 4. card.json exists, same card_id, same card_version as instructions.json
 5. card.json approval.status == "approved" AND approval.approved_version
    equals that card_version (the YES on WhatsApp covered *this* version)
 6. if the caller passed --expect-version (from the YES message), it matches
 7. if instructions.content_sha256 is set, the content still hashes to it
 8. approved_at is present, has a timezone and isn't in the future
 9. every post's platform is known and enabled; schedules are valid
10. every media file exists inside the card folder and is non-empty

Preview mode (used only for dry runs, which never send anything) skips the
approval checks 2, 5 and 8 and also accepts cards in /inbox/, so the exact
final text can be shown *before* the human approves it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from grokbot_publisher.cabinet import CARD, INSTRUCTIONS, Cabinet
from grokbot_publisher.config import ALL_PLATFORMS
from grokbot_publisher.errors import GateRefused, ValidationError
from grokbot_publisher.models import Instructions, valid_card_id
from grokbot_publisher.util import parse_iso, read_json, utcnow

CLOCK_SKEW = timedelta(minutes=5)
_VOLATILE_KEYS = {"approved", "approved_at", "content_sha256"}


@dataclass
class GatePass:
    card_dir: Path
    instructions: Instructions
    card: dict


def fingerprint(card_dir: Path, raw_instructions: dict | None = None) -> str:
    """SHA-256 over the instruction content (minus approval fields) and the
    bytes of every media file. The WhatsApp bridge can store this at YES time;
    any later edit to captions or media then fails the gate."""
    raw = raw_instructions if raw_instructions is not None else read_json(card_dir / INSTRUCTIONS)
    content = {k: v for k, v in raw.items() if k not in _VOLATILE_KEYS}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for item in raw.get("media") or []:
        name = item.get("file") if isinstance(item, dict) else item
        path = card_dir / str(name)
        digest.update(b"\0" + str(name).encode("utf-8") + b"\0")
        if path.is_file():
            with path.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
    return digest.hexdigest()


def check(
    cabinet: Cabinet,
    card_id: str,
    *,
    expect_version: int | None = None,
    enabled_platforms: tuple[str, ...] = ALL_PLATFORMS,
    preview: bool = False,
) -> GatePass:
    reasons: list[str] = []
    allowed_folders = ("approved", "inbox") if preview else ("approved",)

    if not valid_card_id(card_id):
        raise GateRefused([f"invalid card_id {card_id!r}"])
    found = cabinet.find(card_id)
    if found is None:
        raise GateRefused([f"card {card_id} not found in the cabinet"])
    folder, card_dir = found
    if folder not in allowed_folders:
        raise GateRefused([f"card is in /{folder}/, not /approved/"])

    try:
        raw = read_json(card_dir / INSTRUCTIONS)
    except FileNotFoundError:
        raise GateRefused(["instructions.json is missing"]) from None
    except (OSError, ValueError) as exc:
        raise GateRefused([f"instructions.json is unreadable: {exc}"]) from None
    try:
        instr = Instructions.parse(raw)
    except ValidationError as exc:
        raise GateRefused([f"instructions.json: {exc}"]) from None

    # 2. approved must be the boolean true, not "true", 1, "yes"...
    if not preview and instr.approved is not True:
        reasons.append(f'"approved" is {json.dumps(instr.approved)}, not true')

    # 3.
    if instr.card_id != card_id:
        reasons.append(f"instructions card_id {instr.card_id!r} doesn't match folder {card_id!r}")

    # 4 + 5.
    card: dict = {}
    try:
        card = read_json(card_dir / CARD)
    except FileNotFoundError:
        reasons.append("card.json is missing, so the approval record can't be checked")
    except (OSError, ValueError) as exc:
        reasons.append(f"card.json is unreadable: {exc}")
    if card:
        if card.get("card_id") != card_id:
            reasons.append("card.json card_id doesn't match")
        if card.get("card_version") != instr.card_version:
            reasons.append(
                f"card.json is v{card.get('card_version')} but instructions.json is v{instr.card_version} "
                "(card was edited after instructions were written)"
            )
        if not preview:
            reasons += _approval_problems(card, instr.card_version)

    # 6.
    if expect_version is not None and expect_version != instr.card_version:
        reasons.append(f"trigger asked for v{expect_version}, card is v{instr.card_version}")

    # 7.
    if instr.content_sha256:
        actual = fingerprint(card_dir, raw)
        if actual != instr.content_sha256:
            reasons.append("content changed after approval (content_sha256 mismatch)")

    # 8.
    if not preview:
        reasons += _approved_at_problems(instr.approved_at)

    # 9.
    for post in instr.posts:
        if post.platform not in ALL_PLATFORMS:
            reasons.append(f"posts[{post.index}]: unknown platform {post.platform!r}")
        elif post.platform not in enabled_platforms:
            reasons.append(f"posts[{post.index}]: platform {post.platform} is disabled in config")
        try:
            post.due_at()
            post.media_names(instr.media)
        except ValidationError as exc:
            reasons.append(str(exc))

    # 10.
    root = card_dir.resolve()
    for item in instr.media:
        path = (card_dir / item.file).resolve()
        if root not in path.parents:
            reasons.append(f"media '{item.file}' points outside the card folder")
            continue
        if not path.is_file():
            reasons.append(f"media '{item.file}' is missing")
            continue
        if path.stat().st_size == 0:
            reasons.append(f"media '{item.file}' is empty")
            continue
        item.path = path

    if reasons:
        raise GateRefused(reasons)
    return GatePass(card_dir=card_dir, instructions=instr, card=card)


def _approval_problems(card: dict, version: int) -> list[str]:
    """Check 5: the recorded YES covers exactly this version."""
    approval = card.get("approval") if isinstance(card.get("approval"), dict) else {}
    problems = []
    if approval.get("status") != "approved":
        problems.append(f"approval.status is {approval.get('status')!r}, not 'approved'")
    approved_version = approval.get("approved_version")
    if approved_version is None or isinstance(approved_version, bool):
        problems.append("approval.approved_version is not recorded, so there's no proof the YES covered this version")
    elif approved_version != version:
        problems.append(f"YES was given for v{approved_version}, but this is v{version}")
    return problems


def _approved_at_problems(value: str) -> list[str]:
    """Check 8: approved_at exists, has a timezone and isn't in the future."""
    if not value:
        return ["approved_at is missing"]
    try:
        approved_at = parse_iso(value)
    except ValueError as exc:
        return [f"approved_at: {exc}"]
    if approved_at > utcnow() + CLOCK_SKEW:
        return [f"approved_at {value} is in the future"]
    return []
