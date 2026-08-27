"""Provider-agnostic error hierarchy for the LLM layer.

Every adapter maps its transport/protocol failures onto these types, so the
application layer never has to know which API standard is configured.
"""

from typing import Any, Optional


class LLMError(Exception):
    """Base class for every failure raised by the LLM layer."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: Optional[int] = None,
        request_id: str = "",
        raw: Optional[Any] = None,
    ):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.request_id = request_id
        self.raw = raw

    def __str__(self) -> str:
        parts = [self.message]
        if self.provider:
            parts.append(f"provider={self.provider}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


class LLMConfigError(LLMError):
    """Configuration is missing or invalid (unknown standard, empty base_url, ...)."""


class LLMUnsupportedError(LLMError):
    """The selected API standard does not support the requested capability."""


class LLMAuthError(LLMError):
    """401 / 403 — bad or missing API key."""


class LLMBadRequestError(LLMError):
    """400 / 404 / 422 — the request was rejected by the provider."""


class LLMRateLimitError(LLMError):
    """429 — quota or rate limit exhausted."""

    def __init__(self, message: str, *, retry_after_sec: Optional[float] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after_sec = retry_after_sec


class LLMServerError(LLMError):
    """5xx — provider-side failure, safe to retry."""


class LLMTimeoutError(LLMError):
    """The request exceeded the configured timeout."""


class LLMConnectionError(LLMError):
    """The provider endpoint is unreachable."""


class LLMResponseError(LLMError):
    """The response could not be parsed into the unified contract."""
