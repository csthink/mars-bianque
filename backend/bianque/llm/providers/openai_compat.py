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
from bianque.llm.errors import LLMRequestError, RetryableProviderError
from bianque.llm.redacted import RedactedMessage
from bianque.llm.request import ModelRequest
from bianque.llm.types import (
    TOOL_CHOICE_MODES,
    LLMCompletion,
    LLMUsage,
    ToolCall,
    ToolChoice,
    ToolSpec,
    estimate_tokens,
)

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


def _message_payload(message: RedactedMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content.text}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments.text},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _tool_payload(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _tool_choice_payload(choice: ToolChoice) -> Any:
    if choice in TOOL_CHOICE_MODES:
        return choice
    return {"type": "function", "function": {"name": choice}}


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
        api_key = resolve_api_key(settings.llm_api_key)

        options: dict[str, Any] = {
            "api_key": api_key.get_secret_value() if api_key else UNSET_API_KEY_PLACEHOLDER,
            # The timeout comes only from settings, never from the SDK default, so that
            # timeouts are deterministic.
            "timeout": float(settings.llm_timeout_seconds),
            # The gateway owns the retry policy; the adapter never retries.
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

    async def complete(self, request: ModelRequest) -> LLMCompletion:
        """Send one request to the endpoint and return the completion.

        Args:
            request: The request; ``request.model`` names the model.

        Returns:
            The completion. Usage is estimated when the endpoint reports none.

        Raises:
            ValueError: No chat model name is configured.
            RetryableProviderError: Timeout, connection failure, HTTP 429 or 5xx.
            LLMRequestError: Any other rejected request, or a response without choices.
        """
        if not request.model:
            raise ValueError("the openai-compat provider requires BIANQUE_LLM_MODEL to be set")
        options: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_payload(message) for message in request.prompt.messages],
            "temperature": request.temperature,
        }
        if request.tools:
            options["tools"] = [_tool_payload(tool) for tool in request.tools]
            options["tool_choice"] = _tool_choice_payload(request.tool_choice)
        if request.extra_body:
            options["extra_body"] = request.extra_body
        try:
            response = await self._client.chat.completions.create(**options)
        except openai.APITimeoutError as exc:
            raise RetryableProviderError("timeout", "model call timed out") from exc
        except openai.APIConnectionError as exc:
            raise RetryableProviderError("connection", "cannot reach the model service") from exc
        except openai.RateLimitError as exc:
            raise RetryableProviderError("rate_limited", "model service rate limit") from exc
        except openai.InternalServerError as exc:
            raise RetryableProviderError(
                "server_error", f"model service error {exc.status_code}"
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMRequestError(
                f"model service rejected the request ({exc.status_code}): {exc.message}"
            ) from exc
        except (openai.APIResponseValidationError, ValueError) as exc:
            # A 200 response whose body is not JSON, or not the expected shape.
            raise LLMRequestError(f"model service returned an unreadable response: {exc}") from exc
        try:
            choice = response.choices[0]
            text = choice.message.content or ""
            tool_calls = tuple(
                ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
                for call in (choice.message.tool_calls or [])
                if call.type == "function"
            )
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMRequestError(f"model service returned an unexpected response: {exc}") from exc
        usage = response.usage
        if usage is not None:
            llm_usage = LLMUsage(usage.prompt_tokens, usage.completion_tokens)
        else:
            prompt_text = "".join(message.content.text for message in request.prompt.messages)
            output_text = text + "".join(call.arguments for call in tool_calls)
            llm_usage = LLMUsage(
                estimate_tokens(prompt_text), estimate_tokens(output_text), estimated=True
            )
        return LLMCompletion(
            text=text,
            model=response.model or request.model,
            usage=llm_usage,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
        )

    def __repr__(self) -> str:
        """Show only the endpoint host; the credential never appears."""
        return f"OpenAICompatProvider(endpoint_host={self.endpoint_host!r})"
