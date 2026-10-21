"""`grokbot-publisher doctor`: check the setup before the demo, not during it."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from grokbot_publisher.cabinet import FOLDERS
from grokbot_publisher.platforms import REGISTRY
from grokbot_publisher.platforms.base import Context
from grokbot_publisher.storage import make_storage

OK, WARN, FAIL = "ok", "warn", "fail"


def run(publisher, online: bool = False) -> list[tuple[str, str, str]]:
    s = publisher.s
    checks: list[tuple[str, str, str]] = []
    add = lambda level, name, msg: checks.append((level, name, msg))  # noqa: E731

    for binary in (s.ffmpeg, s.ffprobe):
        path = shutil.which(binary)
        if not path:
            add(FAIL, binary, "not found; install ffmpeg (needed for video)")
            continue
        out = subprocess.run([path, "-version"], capture_output=True, text=True).stdout.split("\n", 1)[0]
        add(OK, binary, out[:80])

    missing = [f for f in FOLDERS if not (s.cabinet / f).is_dir()]
    add(WARN if missing else OK, "cabinet",
        f"{s.cabinet} missing {', '.join(missing)} (run `grokbot-publisher init`)" if missing else str(s.cabinet))

    try:
        s.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=s.state_dir):
            pass
        add(OK, "state dir", str(s.state_dir))
    except OSError as exc:
        add(FAIL, "state dir", f"not writable: {exc}")

    if s.tokens_file.exists() and os.name == "posix" and s.tokens_file.stat().st_mode & 0o077:
        add(WARN, "tokens file", f"{s.tokens_file} is readable by other users; chmod 600 it")

    status = publisher.tokens.status()
    for name in s.enabled_platforms:
        st = status.get(name, {})
        if not st.get("access_token"):
            add(FAIL, name, "no access token (run `grokbot-publisher auth url " + name + "`)")
            continue
        exp = st.get("expires_in_hours")
        if exp is not None and exp < 0 and not st.get("refresh_token"):
            add(FAIL, name, f"token expired {-exp:.0f}h ago and can't be refreshed; re-authorise")
        elif exp is not None and exp < 72 and not st.get("refresh_token"):
            add(WARN, name, f"token expires in {exp:.0f}h and can't be refreshed")
        else:
            add(OK, name, f"token present{' (refreshable)' if st.get('refresh_token') else ''}"
                          + (f", account {st['account']}" if st.get("account") else ""))
        if online:
            ctx = Context(settings=s, http=publisher.http, tokens=publisher.tokens, media=publisher.media,
                          storage_factory=publisher.storage_factory, card_id="doctor")
            try:
                who = REGISTRY[name](ctx).whoami()
                add(OK, f"{name} api", f"authenticated as {who.get('account') or who.get('id')}")
            except Exception as exc:
                add(FAIL, f"{name} api", str(exc)[:200])

    needs_storage = "instagram" in s.enabled_platforms or s.tiktok_source == "PULL_FROM_URL"
    try:
        storage = make_storage(s, publisher.http)
        add(OK, "storage", f"{s.storage_backend} configured")
        if online:
            _storage_roundtrip(storage, add)
    except Exception as exc:
        add(FAIL if needs_storage else WARN, "storage", str(exc))
    return checks


def _storage_roundtrip(storage, add) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "grokbot-doctor.txt"
        probe.write_text("grokbot publisher storage check\n")
        try:
            obj = storage.put(probe, "doctor")
        except Exception as exc:
            add(FAIL, "storage upload", str(exc)[:200])
            return
        try:
            resp = requests.get(obj.url, timeout=20)
            if resp.status_code == 200 and b"grokbot" in resp.content:
                add(OK, "storage link", "public link works (platforms can fetch media)")
            else:
                add(FAIL, "storage link", f"link returned HTTP {resp.status_code}; platforms won't be able to fetch")
        except requests.RequestException as exc:
            add(FAIL, "storage link", str(exc)[:200])
        finally:
            storage.delete(obj)
