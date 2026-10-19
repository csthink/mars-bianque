"""Provider for OpenAI-compatible endpoints.

One implementation covers every deployment that speaks the OpenAI-compatible protocol
(hosted APIs, model gateways, vLLM, Ollama); they differ only in ``base_url`` and
credential, so no other SDK is needed.

This subpackage is the only place allowed to import the model SDK (enforced by the
import-linter contract in ``pyproject.toml`` and by ``tests/test_llm_egress_static.py``).

**Credential.** ``BIANQUE_LLM_API_KEY`` holds either the key itself or a reference
``file:<absolute path>``. Settings keep it as ``SecretStr``; it is resolved here into
another ``SecretStr`` and never appears in this class's ``repr`` or ``str``.

**No httpx import.** The timeout is passed to the SDK as seconds (the SDK converts it to
``httpx.Timeout``). Tests that use ``httpx.MockTransport`` pass a whole
``httpx.AsyncClient`` in through ``http_client``, so production code does not import
httpx.

**The SDK client is not public.** ``self._client`` is used only inside this class.
Exposing it through a public attribute would let a caller write
``<provider>.<client>.chat.completions.create(...)`` and send a raw string past the
gateway's gates. Observability needs are met by read-only scalars: ``name``,
``endpoint_host`` and ``timeout_seconds``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import openai
from pydantic import SecretStr

from bianque.config import Settings
from bianque.llm.providers.base import LLMCompletion, LLMUsage
from bianque.llm.redacted import RedactedPrompt

#: Prefix that marks the credential setting as a reference to a file holding the key.
API_KEY_FILE_PREFIX = "file:"

#: Placeholder credential handed to the SDK when ``llm_api_key`` is not set. Two reasons:
#: (1) the SDK rejects an empty credential, while local vLLM or Ollama endpoints often
#: do not check credentials at all, so the placeholder lets those deployments work as
#: is; (2) passing ``api_key=None`` makes the SDK read ``OPENAI_API_KEY`` from the
#: process environment, an implicit credential source outside the ``BIANQUE_``-prefixed
#: settings. The placeholder is not a credential and never refers to one.
UNSET_API_KEY_PLACEHOLDER = "bianque-unset-api-key"


def resolve_api_key(api_key: SecretStr | None) -> SecretStr | None:
    """Resolve the credential setting into the key itself.

    Args:
        api_key: The value of ``BIANQUE_LLM_API_KEY``: the key itself, or
            ``file:<absolute path>`` naming a file that holds the key.

    Returns:
        The key, or ``None`` when the setting is unset or blank.

    Raises:
        ValueError: A ``file:`` reference names a relative path or an empty file.
        OSError: The referenced file cannot be read.
    """
    if api_key is None:
        return None
    value = api_key.get_secret_value().strip()
    if not value:
        return None
    if not value.startswith(API_KEY_FILE_PREFIX):
        return SecretStr(value)
    path = Path(value[len(API_KEY_FILE_PREFIX) :])
    if not path.is_absolute():
        raise ValueError(f"credential file reference must be an absolute path: {path}")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"credential file is empty: {path}")
    return SecretStr(key)


class OpenAICompatProvider:
    """Provider that sends requests to an OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings, *, http_client: Any = None) -> None:
        """Create the SDK client from settings (timeout from settings, resolved credential).

        Args:
            settings: Process settings.
            http_client: Optional ``httpx.AsyncClient`` injected by tests with
                ``MockTransport``. Production code passes ``None`` and the SDK builds
                its own client.
        """
        self._model = settings.llm_model
        api_key = resolve_api_key(settings.llm_api_key)

        options: dict[str, Any] = {
            "api_key": api_key.get_secret_value() if api_key else UNSET_API_KEY_PLACEHOLDER,
            # The timeout comes only from settings, never from the SDK default, so that
            # timeouts are deterministic.
            "timeout": float(settings.llm_timeout_seconds),
            # Retries are decided by the caller's degradation policy, not by the adapter.
            "max_retries": 0,
        }
        if settings.llm_base_url is not None:
            options["base_url"] = settings.llm_base_url
        if http_client is not None:
            options["http_client"] = http_client
        self._client = openai.AsyncOpenAI(**options)

    @property
    def name(self) -> str:
        """The name of this implementation."""
        return "openai-compat"

    @property
    def timeout_seconds(self) -> float | None:
        """The timeout in seconds actually set on the SDK client.

        This reads the timeout from the client object itself rather than from
        ``Settings``: a setting that is never wired to the client would still read back
        correctly from ``Settings``. The SDK client itself stays private (with it a
        caller could bypass the gateway), so only the number is exposed.

        Returns:
            The timeout in seconds, or ``None`` when the client has no timeout.
        """
        raw: Any = self._client.timeout
        if isinstance(raw, int | float):
            return float(raw)
        # httpx.Timeout: take the read timeout (read by attribute; httpx is not imported).
        read: Any = getattr(raw, "read", None)
        return float(read) if isinstance(read, int | float) else None

    @property
    def endpoint_host(self) -> str | None:
        """The host requests are sent to, from the SDK client's ``base_url``."""
        return urlsplit(str(self._client.base_url)).hostname

    async def complete(self, prompt: RedactedPrompt) -> LLMCompletion:
        """Send the redacted chat messages to the endpoint and return the completion.

        Args:
            prompt: The redacted chat messages.

        Returns:
            The completion.

        Raises:
            ValueError: No chat model name is configured.
        """
        if not self._model:
            raise ValueError("the openai-compat provider requires BIANQUE_LLM_MODEL to be set")
        payload: list[Any] = [
            {"role": message.role, "content": message.content.text} for message in prompt.messages
        ]
        response = await self._client.chat.completions.create(model=self._model, messages=payload)
        choice_text = response.choices[0].message.content if response.choices else None
        usage = response.usage
        return LLMCompletion(
            text=choice_text or "",
            model=response.model or self._model,
            usage=LLMUsage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            ),
        )

    def __repr__(self) -> str:
        """Show only the model name and endpoint host; the credential never appears."""
        return f"OpenAICompatProvider(model={self._model!r}, endpoint_host={self.endpoint_host!r})"
