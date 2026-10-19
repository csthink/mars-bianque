"""Provider interface and return types.

The interface is deliberately thin and narrow:

- Inputs are only ``RedactedPrompt``; the redaction type gate extends down to the
  provider layer, and no provider has an overload that accepts a plain ``str``.
- ``endpoint_host`` reports where requests go; ``None`` means the provider has no
  network endpoint.
- All calls are ``async``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bianque.llm.redacted import RedactedPrompt


@dataclass(frozen=True)
class LLMUsage:
    """Token usage of one model call, as reported by the provider.

    Fields are ``0`` when the provider does not report usage.
    """

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class LLMCompletion:
    """The result of one chat completion: text, actual model name and token usage."""

    text: str
    model: str
    usage: LLMUsage


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

    async def complete(self, prompt: RedactedPrompt) -> LLMCompletion:
        """Request one completion for a redacted chat message sequence.

        Args:
            prompt: The redacted chat messages.

        Returns:
            The completion.
        """
        ...
