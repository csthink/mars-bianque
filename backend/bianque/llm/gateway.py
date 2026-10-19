"""The single egress to the model service.

This is the only module in the repository allowed to import ``bianque.llm.providers``.
Every other module, inside the ``bianque.llm`` package or outside it, is forbidden to,
otherwise a second path to the model service exists. ``tests/test_llm_egress_static.py``
enforces this.

## Three gates

``complete()`` is the only egress. Before a request reaches the provider it passes:

1. **Static type gate**: the signature accepts only ``RedactedPrompt``, so mypy strict
   rejects a plain ``str`` at type-check time.
2. **Runtime gate**: an ``isinstance`` check raises ``TypeError`` for anything that is
   not a ``RedactedPrompt`` of ``RedactedText`` messages. This covers dynamic
   construction and deserialization, which bypass static type checking.
3. **Pre-send recheck**: the redaction engine's ``detect()`` runs on every message
   right before sending and raises on any residue. This covers a ``RedactedText``
   forged with the construction sentinel while still holding sensitive text.

## The public surface exposes only scalars

The gateway's public attributes hold no provider object, no HTTP or SDK client and no
transport. Handing out any of them would let a caller write
``gateway.<attribute>.<client>.chat.completions.create(...)`` and send a raw string past
all three gates. Observability needs (which provider, which host) are met by read-only
scalars: ``provider_name`` and ``endpoint_host``. The regression test is
``tests/test_llm_redaction_gate.py::test_gateway_public_surface_exposes_no_network_client``.
"""

from __future__ import annotations

from typing import Any

from bianque.config import Settings
from bianque.llm.providers import (
    LLMCompletion,
    LLMProvider,
    LLMUsage,
    build_provider,
)
from bianque.llm.redacted import (
    RedactedPrompt,
    RedactedText,
    build_redaction_engine,
)
from bianque.redaction import RedactionEngine

#: ``LLMCompletion`` and ``LLMUsage`` are defined by the provider interface and
#: re-exported here, so that callers (including ``bianque.llm``) can use the return
#: types without importing the provider layer.
__all__ = [
    "LLMCompletion",
    "LLMGateway",
    "LLMUsage",
    "RedactionResidueError",
    "build_gateway",
]


class RedactionResidueError(RuntimeError):
    """The pre-send recheck found sensitive residue; nothing was sent."""


class LLMGateway:
    """The single egress to the model service: three gates, then the provider."""

    def __init__(self, provider: LLMProvider, *, engine: RedactionEngine) -> None:
        """Assemble the gateway.

        Args:
            provider: The provider instance.
            engine: The redaction engine used for the pre-send recheck; it must have the
                same configuration as the engine that built the ``RedactedText`` values.
        """
        self._provider = provider
        self._engine = engine

    @property
    def provider_name(self) -> str:
        """The name of the provider implementation behind this gateway.

        Only this scalar is public, never the provider object: the provider holds the
        SDK client, and with it a caller could send requests past ``_guard()``.
        """
        return self._provider.name

    @property
    def endpoint_host(self) -> str | None:
        """The host requests are sent to; ``None`` when the provider has no network endpoint."""
        return self._provider.endpoint_host

    def _require_redacted(self, payload: Any) -> tuple[RedactedText, ...]:
        """Runtime gate: return the redacted texts carried by ``payload`` or raise.

        Args:
            payload: The gateway input.

        Returns:
            Every redacted text in the prompt.

        Raises:
            TypeError: ``payload`` is not a ``RedactedPrompt``, or one of its messages
                carries content that did not go through the redaction pipeline.
        """
        if not isinstance(payload, RedactedPrompt):
            raise TypeError(
                "the model gateway accepts only a RedactedPrompt built by "
                f"redact_prompt_for_llm(), got {type(payload)!r}"
            )
        texts: list[RedactedText] = []
        for index, message in enumerate(payload.messages):
            content: object = message.content
            if not isinstance(content, RedactedText):
                raise TypeError(
                    f"message {index} of the prompt did not go through the redaction "
                    f"pipeline: {type(content)!r}"
                )
            texts.append(content)
        return tuple(texts)

    def _recheck(self, texts: tuple[RedactedText, ...]) -> None:
        """Pre-send recheck: run ``detect()`` on every text and fail closed on residue.

        Args:
            texts: The redacted texts about to be sent.

        Raises:
            RedactionResidueError: A text still contains sensitive content.
        """
        for index, item in enumerate(texts):
            findings = self._engine.detect(item.text)
            if findings:
                rules = sorted({finding.rule for finding in findings})
                raise RedactionResidueError(
                    f"pre-send recheck found sensitive residue in message {index} "
                    f"(rules {rules!r}); the input bypassed redact_for_llm()"
                )

    def _guard(self, payload: Any) -> None:
        """Run the runtime gate and the pre-send recheck.

        Args:
            payload: The gateway input.
        """
        self._recheck(self._require_redacted(payload))

    async def complete(self, prompt: RedactedPrompt) -> LLMCompletion:
        """Request one chat completion after all three gates pass.

        Args:
            prompt: The redacted chat messages.

        Returns:
            The completion.
        """
        self._guard(prompt)
        return await self._provider.complete(prompt)


def build_gateway(settings: Settings, *, http_client: Any = None) -> LLMGateway:
    """Assemble the gateway for the configured model mode.

    Args:
        settings: Process settings.
        http_client: Optional ``httpx.AsyncClient``. Tests inject one backed by
            ``MockTransport``; production code always passes ``None``.

    Returns:
        The gateway.

    Raises:
        ValueError: ``settings.llm_mode`` has no registered provider.
    """
    return LLMGateway(
        build_provider(settings, http_client=http_client),
        engine=build_redaction_engine(settings),
    )
