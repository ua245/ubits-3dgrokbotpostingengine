"""Error taxonomy. Every failure the Publisher can hit maps to exactly one of
these, and each one has a single, fixed handling rule:

    TransientError     -> retry with exponential backoff (bounded)
    AuthExpiredError   -> refresh the token once, then retry
    RateLimitError     -> stop immediately for this platform, never retry
    PermanentError     -> stop for this platform, report the message
    ValidationError    -> the card can't be posted as-is; nothing was sent
    GateRefused        -> safety gate said no; nothing was sent anywhere
"""

from __future__ import annotations


class PublisherError(Exception):
    """Base class for all expected Publisher failures."""

    status = "failed"

    def __init__(self, message: str, *, platform: str | None = None, detail: object = None):
        super().__init__(message)
        self.message = message
        self.platform = platform
        self.detail = detail

    def __str__(self) -> str:
        return self.message


class GateRefused(PublisherError):
    status = "refused"

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class ValidationError(PublisherError):
    status = "invalid"


class ConfigError(PublisherError):
    status = "not_configured"


class TransientError(PublisherError):
    """Network blips, 5xx, 'try again' responses. Safe to retry."""


class AuthExpiredError(PublisherError):
    """Access token expired or was rejected. Refresh once, then retry."""


class RateLimitError(PublisherError):
    """Platform quota or rate cap hit. Never retried automatically."""

    status = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class PermanentError(PublisherError):
    """The platform rejected the request; retrying won't help."""


class ManualActionRequired(PublisherError):
    """The API can't do this (e.g. TikTok has no delete endpoint)."""

    status = "manual_action_required"
