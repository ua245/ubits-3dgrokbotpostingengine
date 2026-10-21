"""Append-only log of every publish and takedown (JSON Lines).

It lives in the cabinet's .publisher/ folder, not in the card folder, so it
survives Filing moving cards around. It is the source of truth for
"has this already been posted?" and for "what do I delete?".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from grokbot_publisher.util import iso


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, event: str, **fields) -> dict:
        entry = {"ts": iso(), "event": event, **{k: v for k, v in fields.items() if v not in (None, "")}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    def entries(self) -> Iterator[dict]:
        if not self.path.is_file():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue  # a torn last line after a crash; skip it

    def live_posts(self, card_id: str | None = None, platform: str | None = None) -> list[dict]:
        """Published posts that haven't been taken down, newest last."""
        live: dict[tuple, dict] = {}
        for e in self.entries():
            if card_id and e.get("card_id") != card_id:
                continue
            if platform and e.get("platform") != platform:
                continue
            key = (e.get("card_id"), e.get("post_key"), e.get("post_id"))
            if e.get("event") == "published":
                live[key] = e
            elif e.get("event") == "deleted":
                live.pop(key, None)
        return list(live.values())

    def published_for(self, card_id: str, post_key: str) -> dict | None:
        """The live publish entry for this post of this card, if any."""
        for entry in reversed(self.live_posts(card_id)):
            if entry.get("post_key") == post_key:
                return entry
        return None
