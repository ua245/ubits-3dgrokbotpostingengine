from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from grokbot_publisher.config import Settings
from grokbot_publisher.http import Http
from grokbot_publisher.media import MediaTool
from grokbot_publisher.platforms.base import Context
from grokbot_publisher.storage import StoredObject
from grokbot_publisher.tokens import TokenStore

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")

CARD_ID = "20261021-1849-ubits-launch"


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("media")
    if HAS_FFMPEG:
        # Compliant vertical clip: h264/aac, faststart.
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=4",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
               "-movflags", "+faststart", str(d / "vertical.mp4"))
        # Same but without faststart (moov at the end).
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(d / "slowstart.mp4"))
        # Landscape 720p clip.
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
               "-movflags", "+faststart", str(d / "landscape.mp4"))
    from PIL import Image

    Image.new("RGBA", (1200, 1200), (255, 0, 0, 128)).save(d / "alpha.png")
    Image.new("RGB", (3000, 1000), (0, 128, 255)).save(d / "wide.jpg", quality=90)
    Image.new("RGB", (1080, 1080), (10, 200, 10)).save(d / "square.jpg", quality=90)
    return d


@pytest.fixture
def settings(tmp_path) -> Settings:
    env = {
        "GROKBOT_CABINET": str(tmp_path / "cabinet"),
        "PUBLISHER_STATE_DIR": str(tmp_path / "state"),
        "PUBLISHER_TOKENS_FILE": str(tmp_path / "tokens.json"),
        "PUBLISHER_MAX_RETRIES": "2",
        "PUBLISHER_POLL_TIMEOUT": "60",
        "X_CLIENT_ID": "xcid",
        "GOOGLE_CLIENT_ID": "gcid",
        "GOOGLE_CLIENT_SECRET": "gsecret",
        "TIKTOK_CLIENT_KEY": "tkey",
        "TIKTOK_CLIENT_SECRET": "tsecret",
    }
    return Settings.from_env(env=env)


@pytest.fixture
def tokens(settings) -> TokenStore:
    store = TokenStore(settings.tokens_file, env={})
    for name in ("instagram", "x", "youtube", "linkedin", "tiktok"):
        store.update(name, access_token=f"{name}-token", refresh_token=f"{name}-refresh", expires_in=86400)
    store.update("instagram", user_id="1784", expires_in=60 * 86400)  # long-lived IG token
    store.update("linkedin", user_id="abc123")
    store.update("x", username="ubitstech")
    return store


@pytest.fixture
def http() -> Http:
    return Http(timeout=5, max_retries=2, backoff_base=0.01, sleep=lambda s: None)


class FakeStorage:
    def __init__(self):
        self.put_calls: list[Path] = []
        self.deleted: list[str] = []

    def put(self, path: Path, card_id: str) -> StoredObject:
        self.put_calls.append(path)
        key = f"tmp/{card_id}/{path.name}"
        return StoredObject(key=key, url=f"https://bucket.example/{key}?sig=1", backend="fake")

    def delete(self, obj: StoredObject) -> None:
        self.deleted.append(obj.key)


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def ctx(settings, http, tokens, storage) -> Context:
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    return Context(settings=settings, http=http, tokens=tokens, media=MediaTool(settings),
                   storage_factory=lambda: storage, card_id=CARD_ID, sleep=sleep, clock=lambda: clock["t"])


def make_card(cabinet: Path, *, card_id: str = CARD_ID, folder: str = "approved", version: int = 3,
              approved=True, approved_version=3, posts=None, media=None, files=None, card_version=None,
              **instr_extra) -> Path:
    """Write a card folder; returns its path."""
    d = cabinet / folder / card_id
    d.mkdir(parents=True, exist_ok=True)
    for name, src in (files or {}).items():
        shutil.copy(src, d / name)
    instructions = {
        "card_id": card_id,
        "card_version": version,
        "approved": approved,
        "approved_at": "2026-10-21T19:02:00Z",
        "media": media if media is not None else [],
        "posts": posts if posts is not None else [{"platform": "x", "text": "hello", "schedule": "now"}],
        **instr_extra,
    }
    card = {
        "card_id": card_id,
        "card_version": version if card_version is None else card_version,
        "approval": {"status": "approved", "approved_version": approved_version},
        "history": [],
    }
    (d / "instructions.json").write_text(json.dumps(instructions))
    (d / "card.json").write_text(json.dumps(card))
    return d
