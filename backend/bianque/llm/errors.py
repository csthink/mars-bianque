"""Errors raised by the model gateway.

Callers degrade on these (fall back to a default plan, a rule-based judgement or a
degraded report); the gateway never swallows them.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base class of model gateway errors."""


class RetryableProviderError(LLMError):
    """A provider failure worth retrying: timeout, connection failure, HTTP 429 or 5xx.

    Raised by providers and handled by the gateway's retry loop; callers see
    ``LLMUnavailableError`` once retries are exhausted.
    """

    def __init__(self, category: str, message: str) -> None:
        """Keep the failure category next to the message.

        Args:
            category: ``timeout``, ``connection``, ``rate_limited`` or ``server_error``.
            message: What failed.
        """
        super().__init__(message)
        self.category = category


class LLMUnavailableError(LLMError):
    """The model service kept failing with retryable errors until retries ran out."""

    def __init__(self, category: str, attempts: int) -> None:
        """Record the last failure category and how many attempts were made."""
        super().__init__(f"model service unavailable ({category}) after {attempts} attempts")
        self.category = category
        self.attempts = attempts


class LLMRequestError(LLMError):
    """The model service rejected the request, or its response could not be used."""


class RecordingNotFoundError(LLMError):
    """``recorded`` mode found no recorded response for a request key."""


class RecordingFormatError(LLMError):
    """A recording file has an unsupported schema version or an invalid structure."""


class StructuredOutputError(LLMError):
    """Structured output stayed invalid after the correction retry."""
