"""Provider interface.

The interface is deliberately thin and narrow:

- The input is a ``ModelRequest`` whose prompt is a ``RedactedPrompt``; the redaction
  type gate extends down to the provider layer, and no provider accepts a plain ``str``.
- ``endpoint_host`` reports where requests go; ``None`` means the provider has no
  network endpoint.
- Providers do not retry. They raise ``RetryableProviderError`` for failures worth
  retrying and leave the retry policy to the gateway.
- All calls are ``async``.
"""

from __future__ import annotations

from typing import Protocol

from bianque.llm.request import ModelRequest
from bianque.llm.types import LLMCompletion


class LLMProvider(Protocol):
    """Provider interface. Code outside the provider layer depends only on this."""

    @property
    def name(self) -> str:
        """The name of this implementation."""
        ...

    @property
    def endpoint_host(self) -> str | None:
        """The host requests are sent to; ``None`` for an implementation with no network."""
        ...

    async def complete(self, request: ModelRequest) -> LLMCompletion:
        """Answer one model request.

        Args:
            request: The request, with a redacted prompt.

        Returns:
            The completion.
        """
        ...
