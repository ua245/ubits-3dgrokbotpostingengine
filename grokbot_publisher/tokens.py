"""Token store: one JSON file (chmod 600), one section per platform.

    {"x": {"access_token": "...", "refresh_token": "...", "expires_at": 1761072000,
           "username": "ubitstech"}, ...}

Env vars like X_ACCESS_TOKEN override nothing in the file but fill in when the
file has no entry, which is handy for a first test. Refreshed tokens are
written back immediately under a lock: X rotates refresh tokens, so losing
the new one means re-authorising by hand.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from grokbot_publisher.errors import AuthExpiredError, ConfigError, PublisherError
from grokbot_publisher.util import atomic_write_json, file_lock, read_json

log = logging.getLogger("grokbot_publisher.tokens")

ENV_PREFIX = {"instagram": "INSTAGRAM", "x": "X", "youtube": "YOUTUBE", "linkedin": "LINKEDIN", "tiktok": "TIKTOK"}


class TokenStore:
    def __init__(self, path: Path, env: dict | None = None):
        self.path = Path(path)
        self.env = dict(os.environ) if env is None else env

    @property
    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = read_json(self.path)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"token file {self.path} is unreadable: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def get(self, platform: str) -> dict:
        entry = dict(self._read().get(platform) or {})
        prefix = ENV_PREFIX.get(platform, platform.upper())
        for field in ("access_token", "refresh_token", "user_id", "username"):
            env_value = self.env.get(f"{prefix}_{field.upper()}")
            if env_value and not entry.get(field):
                entry[field] = env_value
        return entry

    def require(self, platform: str) -> dict:
        entry = self.get(platform)
        if not entry.get("access_token"):
            raise ConfigError(
                f"{platform}: no access token. Run `grokbot-publisher auth url {platform}` "
                f"or set {ENV_PREFIX.get(platform, platform.upper())}_ACCESS_TOKEN",
                platform=platform,
            )
        return entry

    def update(self, platform: str, **fields) -> dict:
        with file_lock(self._lock_path, wait=10):
            data = self._read()
            entry = dict(data.get(platform) or {})
            entry.update({k: v for k, v in fields.items() if v is not None})
            if "expires_in" in entry:
                entry["expires_at"] = int(time.time()) + int(entry.pop("expires_in"))
            if "refresh_expires_in" in entry:
                entry["refresh_expires_at"] = int(time.time()) + int(entry.pop("refresh_expires_in"))
            entry["updated_at"] = int(time.time())
            data[platform] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, data, mode=0o600)
        return entry

    def status(self) -> dict:
        """What's configured, without revealing any secret."""
        out = {}
        now = time.time()
        for platform in ENV_PREFIX:
            entry = self.get(platform)
            exp = entry.get("expires_at")
            out[platform] = {
                "access_token": bool(entry.get("access_token")),
                "refresh_token": bool(entry.get("refresh_token")),
                "expires_in_hours": round((exp - now) / 3600, 1) if exp else None,
                "account": entry.get("username") or entry.get("user_id") or "",
            }
        return out


class BearerAuth:
    """Bearer-token auth with proactive and on-401 refresh.

    `refresher` is a function(entry) -> dict of new token fields, or None if
    this platform can't refresh (then a 401 is reported as 're-auth needed').
    """

    def __init__(self, store: TokenStore, platform: str, refresher=None, early: float = 300.0,
                 param: str | None = None):
        self.store = store
        self.platform = platform
        self.refresher = refresher
        self.early = early
        self.param = param  # send as a query param instead of a header (Instagram)
        self.entry = store.require(platform)
        if self._expiring() and refresher is not None:
            try:
                self.refresh()
            except Exception as exc:
                # A failed *early* refresh is fine while the old token still works.
                if time.time() >= float(self.entry.get("expires_at") or 0):
                    raise
                log.warning("%s: early token refresh failed, using current token: %s", platform, exc)

    def _expiring(self) -> bool:
        exp = self.entry.get("expires_at")
        return bool(exp) and time.time() > float(exp) - self.early

    @property
    def token(self) -> str:
        return self.entry["access_token"]

    def apply(self, headers: dict, params: dict) -> None:
        if self.param:
            params[self.param] = self.token
        else:
            headers["Authorization"] = f"Bearer {self.token}"

    def refresh(self) -> bool:
        if self.refresher is None:
            return False
        with file_lock(self.store.path.with_name(f"{self.store.path.name}.{self.platform}.refresh.lock"), wait=30):
            # Another process may have refreshed while we waited.
            latest = self.store.get(self.platform)
            if latest.get("access_token") != self.entry.get("access_token"):
                self.entry = latest
                return True
            try:
                new_fields = self.refresher(latest)
            except PublisherError as exc:
                raise AuthExpiredError(
                    f"{self.platform}: token refresh was rejected ({exc}); "
                    f"re-run `grokbot-publisher auth url {self.platform}`",
                    platform=self.platform,
                ) from exc
            if not new_fields or not new_fields.get("access_token"):
                raise AuthExpiredError(
                    f"{self.platform}: token refresh failed; re-run `grokbot-publisher auth url {self.platform}`",
                    platform=self.platform,
                )
            self.entry = self.store.update(self.platform, **new_fields)
        return True
