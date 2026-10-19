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

from collections.abc import Callable
from typing import Any, Final

from bianque.config import Settings
from bianque.llm.providers.base import LLMCompletion, LLMProvider, LLMUsage
from bianque.llm.providers.openai_compat import OpenAICompatProvider
from bianque.llm.providers.stub import StubProvider


def _build_live(settings: Settings, http_client: Any) -> LLMProvider:
    return OpenAICompatProvider(settings, http_client=http_client)


def _build_scripted(settings: Settings, http_client: Any) -> LLMProvider:
    # The stub has no network access, so an injected HTTP client is meaningless to it.
    return StubProvider(settings)


#: Provider registry keyed by ``Settings.llm_mode`` (``BIANQUE_LLM_MODE``).
#: ``recorded`` has no provider yet; selecting it fails in ``build_provider()``.
PROVIDER_REGISTRY: Final[dict[str, Callable[[Settings, Any], LLMProvider]]] = {
    "live": _build_live,
    "scripted": _build_scripted,
}


def build_provider(settings: Settings, *, http_client: Any = None) -> LLMProvider:
    """Build the provider registered for ``settings.llm_mode``.

    Args:
        settings: Process settings.
        http_client: Optional ``httpx.AsyncClient``. Tests inject one backed by
            ``MockTransport``; production code always passes ``None``.

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
    return factory(settings, http_client)


__all__ = [
    "PROVIDER_REGISTRY",
    "LLMCompletion",
    "LLMProvider",
    "LLMUsage",
    "OpenAICompatProvider",
    "StubProvider",
    "build_provider",
]
