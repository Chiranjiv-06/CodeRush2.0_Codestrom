"""Structured Zerion errors.

Every failure a caller can see carries a stable ``code`` so an agent can branch
on it without parsing prose, and a message that has already been stripped of
anything a provider response might have echoed back at us.
"""
from __future__ import annotations

import re
from typing import Any

# Anything that looks like a credential is removed from provider text before it
# reaches a log line, a job result or an API response. Belt and braces: the
# clients never put a secret into a message in the first place.
_REDACTIONS = (
    re.compile(r"zk_[A-Za-z0-9_\-]{6,}"),                 # Zerion API keys
    re.compile(r"0x[a-fA-F0-9]{64}"),                     # EVM private keys
    re.compile(r"(?i)\b(authorization|api[_-]?key|private[_-]?key|secret|token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)basic\s+[A-Za-z0-9+/=]{8,}"),        # Basic auth headers
)

MAX_DETAIL_CHARS = 400


def sanitize(text: Any) -> str:
    """Redact credential-shaped substrings and cap length."""
    out = str(text or "")
    for pattern in _REDACTIONS:
        out = pattern.sub("[redacted]", out)
    out = " ".join(out.split())
    return out[:MAX_DETAIL_CHARS]


class ZerionError(Exception):
    """Base class for every Zerion integration failure."""

    code = "zerion_error"
    http_status = 502
    retryable = False

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(sanitize(message) or self.code)
        self.context = {k: v for k, v in context.items() if v is not None}

    @property
    def detail(self) -> str:
        return str(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "provider": "zerion",
            "detail": self.detail,
            "retryable": self.retryable,
            **self.context,
        }


class ZerionDisabledError(ZerionError):
    code = "zerion_disabled"
    http_status = 503


class ZerionConfigError(ZerionError):
    """No usable credential for any supported rail."""

    code = "zerion_not_configured"
    http_status = 503


class ZerionValidationError(ZerionError, ValueError):
    """Bad wallet address, unknown capability, or a disallowed chain.

    Also a ``ValueError``: that is the contract the external-service registry
    validates against, and it is what turns a malformed request into a 400
    before a job is priced rather than a 500 after.
    """

    code = "zerion_invalid_request"
    http_status = 400


class ZerionQuotaError(ZerionError):
    """A quota would be exceeded. Raised *before* any paid request is made."""

    code = "zerion_quota_exceeded"
    http_status = 429


class ZerionBudgetError(ZerionError):
    """The caller cannot afford the request. Raised before paying."""

    code = "zerion_budget_exceeded"
    http_status = 402


class ZerionPaymentError(ZerionError):
    """The Zerion-side payment could not be authorized or settled."""

    code = "zerion_payment_failed"
    http_status = 402


class ZerionTimeoutError(ZerionError):
    code = "zerion_timeout"
    http_status = 504
    retryable = True


class ZerionRateLimitError(ZerionError):
    code = "zerion_rate_limited"
    http_status = 429
    retryable = True


class ZerionAuthError(ZerionError):
    code = "zerion_unauthorized"
    http_status = 502


class ZerionUnavailableError(ZerionError):
    """Provider unreachable, or the configured transport is not installed."""

    code = "zerion_unavailable"
    http_status = 503
    retryable = True


class ZerionResponseError(ZerionError):
    """Provider answered with something we cannot normalize."""

    code = "zerion_bad_response"
    http_status = 502
