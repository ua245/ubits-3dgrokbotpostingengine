"""The filing cabinet on disk.

    grokbot-cabinet/
      inbox/ approved/ published/ tasks/ deleted/ style/
        <card_id>/card.json, instructions.json, media..., publish_report.json
      .publisher/            <- Publisher-only state (ledger, locks, media cache)

The Publisher only *reads* cards from approved/. It writes exactly two things
into a card folder: publish_report.json and one history line in card.json.
Moving cards between folders is Filing's job (unless --move-on-success).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from grokbot_publisher.errors import ValidationError
from grokbot_publisher.models import valid_card_id
from grokbot_publisher.util import atomic_write_json, iso, read_json

FOLDERS = ("inbox", "approved", "published", "tasks", "deleted", "style")
INSTRUCTIONS = "instructions.json"
CARD = "card.json"
REPORT = "publish_report.json"


class Cabinet:
    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure(self) -> None:
        for name in FOLDERS:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def folder(self, name: str) -> Path:
        return self.root / name

    def card_dir(self, card_id: str, folder: str = "approved") -> Path:
        if not valid_card_id(card_id):
            raise ValidationError(f"invalid card_id {card_id!r} (expected YYYYMMDD-HHMM-short-slug)")
        return self.root / folder / card_id

    def find(self, card_id: str) -> tuple[str, Path] | None:
        """Which folder is this card in right now?"""
        for name in FOLDERS:
            path = self.card_dir(card_id, name)
            if path.is_dir():
                return name, path
        return None

    def approved_cards(self) -> list[Path]:
        base = self.folder("approved")
        if not base.is_dir():
            return []
        return sorted(p for p in base.iterdir() if p.is_dir() and valid_card_id(p.name))

    @staticmethod
    def load_report(card_dir: Path) -> dict | None:
        path = card_dir / REPORT
        if not path.is_file():
            return None
        try:
            return read_json(path)
        except (OSError, ValueError):
            return None

    @staticmethod
    def write_report(card_dir: Path, report: dict) -> Path:
        path = card_dir / REPORT
        atomic_write_json(path, report)
        return path

    @staticmethod
    def append_history(card_dir: Path, message: str) -> None:
        """One timestamped line in card.json history, as every component does."""
        path = card_dir / CARD
        if not path.is_file():
            return
        try:
            card = read_json(path)
        except (OSError, json.JSONDecodeError):
            return
        history = card.get("history")
        if not isinstance(history, list):
            history = []
        history.append(f"{iso()} publisher: {message}")
        card["history"] = history
        atomic_write_json(path, card)

    def move(self, card_id: str, src: str, dst: str) -> Path:
        source = self.card_dir(card_id, src)
        target = self.card_dir(card_id, dst)
        if target.exists():
            raise ValidationError(f"{target} already exists; not overwriting")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return target
