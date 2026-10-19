"""Deterministic provider without network access, used in ``scripted`` mode.

It is a real implementation of the provider interface, not a test double: the same
input always yields the same output (derived from the SHA-256 of the input, with no
randomness and no clock), it holds no network client, and ``endpoint_host`` is always
``None``.
"""

from __future__ import annotations

import hashlib
import math

from bianque.config import Settings
from bianque.llm.providers.base import LLMCompletion, LLMUsage
from bianque.llm.redacted import RedactedPrompt

#: Model name reported when no model name is configured.
DEFAULT_STUB_MODEL = "stub-model"

#: Characters per token used for the stub's reported usage. Roughly right for English
#: prose; the stub only needs a deterministic count.
_CHARS_PER_TOKEN = 4


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _token_count(text: str) -> int:
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


class StubProvider:
    """Provider with no network access and deterministic output."""

    def __init__(self, settings: Settings) -> None:
        """Record the model name to report; there is no network client to create.

        Args:
            settings: Process settings; the stub default model name is used when none
                is configured.
        """
        self._model = settings.llm_model or DEFAULT_STUB_MODEL

    @property
    def name(self) -> str:
        """The name of this implementation."""
        return "stub"

    @property
    def endpoint_host(self) -> str | None:
        """Always ``None``: the stub has no network endpoint."""
        return None

    async def complete(self, prompt: RedactedPrompt) -> LLMCompletion:
        """Return a completion determined only by the input.

        Args:
            prompt: The redacted chat messages.

        Returns:
            The completion; its text is ``stub-completion:<first 16 hex digits of the
            input digest>``.
        """
        joined = "\n".join(message.content.text for message in prompt.messages)
        text = f"stub-completion:{_digest(joined)[:16]}"
        return LLMCompletion(
            text=text,
            model=self._model,
            usage=LLMUsage(
                prompt_tokens=_token_count(joined),
                completion_tokens=_token_count(text),
            ),
        )

    def __repr__(self) -> str:
        """Show only the model name; the stub holds no credential."""
        return f"StubProvider(model={self._model!r})"
