"""One HTTP client with one retry policy, shared by every platform module.

Rules (the same for every platform, every time):

* Transient errors (network blips, 5xx) are retried with exponential backoff
  and jitter, up to `max_retries` times.
* Requests that create something (non-idempotent) are only retried when the
  request provably never reached the server, or the server said 503. Anything
  else could double-post, so it is reported instead.
* An expired/invalid token triggers exactly one refresh, then one retry.
* Rate limits stop immediately. They are never retried.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Protocol

import requests

from grokbot_publisher.errors import (
    AuthExpiredError,
    PermanentError,
    PublisherError,
    RateLimitError,
    TransientError,
)
from grokbot_publisher.util import redact

log = logging.getLogger("grokbot_publisher.http")

Classifier = Callable[[requests.Response, str], PublisherError | None]


class Auth(Protocol):
    def apply(self, headers: dict, params: dict) -> None: ...

    def refresh(self) -> bool: ...


def body_text(resp: requests.Response, limit: int = 500) -> str:
    try:
        text = resp.text
    except Exception:  # pragma: no cover - defensive
        return ""
    return redact(text[:limit])


def retry_after_seconds(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return float(value)
        except ValueError:
            return None
    reset = resp.headers.get("x-rate-limit-reset")  # X sends an epoch timestamp
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            return None
    return None


def default_classify(resp: requests.Response, platform: str) -> PublisherError | None:
    code = resp.status_code
    if 200 <= code < 300 or code == 308:
        return None
    msg = f"{platform}: HTTP {code}: {body_text(resp)}"
    if code == 401:
        return AuthExpiredError(msg, platform=platform)
    if code == 429:
        return RateLimitError(msg, platform=platform, retry_after=retry_after_seconds(resp))
    if code in (408, 409) or code >= 500:
        return TransientError(msg, platform=platform, detail={"status": code})
    return PermanentError(msg, platform=platform, detail={"status": code})


def _never_sent(exc: requests.RequestException) -> bool:
    """True when the failure happened before any bytes reached the server."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    text = repr(exc)
    return any(
        marker in text
        for marker in ("NewConnectionError", "NameResolutionError", "Failed to resolve", "Connection refused")
    )


class Http:
    def __init__(
        self,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        backoff_cap: float = 30.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "grokbot-publisher/0.1")
        self.sleep = sleep

    @classmethod
    def from_settings(cls, settings, **kw) -> Http:
        return cls(
            timeout=settings.http_timeout,
            max_retries=settings.max_retries,
            backoff_base=settings.backoff_base,
            backoff_cap=settings.backoff_cap,
            **kw,
        )

    def backoff(self, attempt: int, hint: float | None = None) -> float:
        if hint is not None and 0 < hint <= self.backoff_cap:
            return hint
        delay = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        return delay * (0.5 + random.random() / 2)

    def request(
        self,
        method: str,
        url: str,
        *,
        platform: str,
        classify: Classifier = default_classify,
        idempotent: bool | None = None,
        auth: Auth | None = None,
        retries: int | None = None,
        basic_auth: tuple[str, str] | None = None,
        **kwargs,
    ) -> requests.Response:
        if idempotent is None:
            idempotent = method.upper() in {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
        kwargs.setdefault("timeout", self.timeout)
        if basic_auth is not None:
            kwargs["auth"] = basic_auth
        # A callable producing the body, so a file upload can be re-opened on retry.
        body_factory = kwargs.pop("body_factory", None)
        base_headers = dict(kwargs.pop("headers", None) or {})
        base_params = dict(kwargs.pop("params", None) or {})
        max_retries = self.max_retries if retries is None else retries
        attempt = 0
        refreshed = False

        while True:
            headers, params = dict(base_headers), dict(base_params)
            if auth is not None:
                auth.apply(headers, params)
            body = body_factory() if body_factory is not None else None
            send = dict(kwargs, data=body) if body_factory is not None else kwargs
            try:
                resp = self.session.request(method, url, headers=headers, params=params or None, **send)
            except requests.RequestException as exc:
                safe = idempotent or _never_sent(exc)
                err = TransientError(
                    f"{platform}: network error on {method} {redact(url)}: {redact(exc)}"
                    + ("" if safe else " (request may have reached the server; not retried to avoid a duplicate post)"),
                    platform=platform,
                )
                if safe and attempt < max_retries:
                    delay = self.backoff(attempt)
                    log.warning("%s; retry %d/%d in %.1fs", err, attempt + 1, max_retries, delay)
                    self.sleep(delay)
                    attempt += 1
                    continue
                raise err from exc
            finally:
                if hasattr(body, "close"):
                    body.close()

            err = classify(resp, platform)
            if err is None:
                return resp

            if isinstance(err, RateLimitError):
                raise err
            if isinstance(err, AuthExpiredError):
                if auth is not None and not refreshed and auth.refresh():
                    refreshed = True
                    log.info("%s: token refreshed, retrying once", platform)
                    continue
                raise err
            if isinstance(err, TransientError):
                detail = err.detail if isinstance(err.detail, dict) else {}
                # 503 = not processed; safe_retry = platform said nothing was created.
                safe = idempotent or detail.get("status") == 503 or bool(detail.get("safe_retry"))
                if safe and attempt < max_retries:
                    delay = self.backoff(attempt, retry_after_seconds(resp))
                    log.warning("%s; retry %d/%d in %.1fs", err, attempt + 1, max_retries, delay)
                    self.sleep(delay)
                    attempt += 1
                    continue
                if not safe:
                    err.message += " (not retried: the platform may have accepted it; check before re-running)"
            raise err


def json_body(resp: requests.Response) -> dict:
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError as exc:
        raise PermanentError(f"unexpected non-JSON response: {body_text(resp)}") from exc
    return data if isinstance(data, dict) else {"data": data}
