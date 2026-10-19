"""Provider implementations and the registry that selects one by model mode.

This subpackage is the only place allowed to import the model SDK (``openai``). In
addition, no module except ``bianque.llm.gateway`` may import this subpackage. Together
these two rules keep a single egress to the model service; they are enforced by the
import-linter contract in ``pyproject.toml`` and by ``tests/test_llm_egress_static.py``.

``build_provider()`` is where the model mode takes effect: code outside the provider
layer receives only the gateway and never branches on the mode. Adding a mode means
adding one registry entry.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from bianque.config import Settings
from bianque.llm.providers.base import LLMProvider
from bianque.llm.providers.openai_compat import OpenAICompatProvider
from bianque.llm.providers.recorded import RecordedProvider
from bianque.llm.providers.scripted import ScriptedProvider
from bianque.llm.recording import write_recording
from bianque.llm.request import ModelRequest
from bianque.llm.scripted import ScriptedRule
from bianque.llm.types import LLMCompletion


class RecordingProvider:
    """Wraps the ``live`` provider and writes a recording of every successful call."""

    def __init__(self, inner: LLMProvider, directory: Path) -> None:
        """Wrap ``inner``; recordings go to ``directory``."""
        self._inner = inner
        self._directory = directory

    @property
    def name(self) -> str:
        """The wrapped provider's name."""
        return self._inner.name

    @property
    def endpoint_host(self) -> str | None:
        """The wrapped provider's endpoint host."""
        return self._inner.endpoint_host

    async def complete(self, request: ModelRequest) -> LLMCompletion:
        """Call the wrapped provider, then record the request and response."""
        completion = await self._inner.complete(request)
        write_recording(self._directory, request, completion)
        return completion


@dataclass(frozen=True)
class ProviderOptions:
    """What a provider factory may use besides settings."""

    #: Optional ``httpx.AsyncClient`` injected by tests with ``MockTransport``.
    http_client: Any = None
    #: Rules for ``scripted`` mode, tried before the built-in fallback rule.
    scripted_rules: Sequence[ScriptedRule] = ()


def _build_live(settings: Settings, options: ProviderOptions) -> LLMProvider:
    provider: LLMProvider = OpenAICompatProvider(settings, http_client=options.http_client)
    if settings.llm_record_dir is not None:
        provider = RecordingProvider(provider, settings.llm_record_dir)
    return provider


def _build_recorded(settings: Settings, options: ProviderOptions) -> LLMProvider:
    return RecordedProvider(settings.llm_recordings_dir)


def _build_scripted(settings: Settings, options: ProviderOptions) -> LLMProvider:
    return ScriptedProvider(options.scripted_rules)


#: Provider registry keyed by ``Settings.llm_mode`` (``BIANQUE_LLM_MODE``).
PROVIDER_REGISTRY: Final[dict[str, Callable[[Settings, ProviderOptions], LLMProvider]]] = {
    "live": _build_live,
    "recorded": _build_recorded,
    "scripted": _build_scripted,
}


def build_provider(settings: Settings, options: ProviderOptions | None = None) -> LLMProvider:
    """Build the provider registered for ``settings.llm_mode``.

    Args:
        settings: Process settings.
        options: Test hooks and scripted rules; production code passes ``None``.

    Returns:
        The provider.

    Raises:
        ValueError: No provider is registered for ``settings.llm_mode``.
    """
    factory = PROVIDER_REGISTRY.get(settings.llm_mode)
    if factory is None:
        raise ValueError(
            f"LLM mode {settings.llm_mode!r} is not available; "
            f"available modes: {sorted(PROVIDER_REGISTRY)}"
        )
    return factory(settings, options or ProviderOptions())


__all__ = [
    "PROVIDER_REGISTRY",
    "LLMProvider",
    "OpenAICompatProvider",
    "ProviderOptions",
    "RecordedProvider",
    "RecordingProvider",
    "ScriptedProvider",
    "build_provider",
]
