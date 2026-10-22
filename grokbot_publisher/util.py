"""Small shared helpers: time, atomic file writes, locking, secret redaction."""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. Naive timestamps are rejected: a post time
    without a timezone is ambiguous, and ambiguity is not allowed at this step."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no timezone (add Z or +01:00)")
    return dt


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Write via temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, data: object, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=mode)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class LockBusy(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path, stale_after: float = 6 * 3600, wait: float = 0.0) -> Iterator[None]:
    """Cross-platform exclusive lock using O_EXCL lock files.

    Works on Linux, macOS and Windows. A lock older than `stale_after`
    seconds, or held by a dead PID on this host, is treated as abandoned.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    host = socket.gethostname()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if _lock_is_stale(path, stale_after, host):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise LockBusy(f"{path.name} is locked by another Publisher run") from None
            time.sleep(0.2)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": os.getpid(), "host": host, "ts": time.time()}, fh)
        yield
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _lock_is_stale(path: Path, stale_after: float, host: str) -> bool:
    try:
        info = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        # Unreadable or half-written: only stale if old.
        try:
            return time.time() - path.stat().st_mtime > stale_after
        except FileNotFoundError:
            return True
    if time.time() - float(info.get("ts", 0)) > stale_after:
        return True
    if info.get("host") == host and info.get("pid"):
        return not _pid_alive(int(info["pid"]))
    return False


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


_SECRET_PARAM = re.compile(
    r"(?i)((?:access_token|refresh_token|client_secret|code|X-Amz-Signature|X-Amz-Credential)=)[^&\s\"']+"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact(text: str) -> str:
    """Strip tokens and signatures out of anything that might be logged."""
    text = _SECRET_PARAM.sub(r"\1***", str(text))
    return _BEARER.sub(r"\1***", text)
