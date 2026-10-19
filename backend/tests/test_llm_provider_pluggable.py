"""Pluggable providers: switching the model mode or endpoint is a configuration change.

The same call sequence runs under several configurations and returns structurally
equivalent results, with no change to the calling code. "No change" takes a concrete
form here: every test uses the same module-level function object ``_call_sequence``,
which depends only on ``LLMGateway`` and contains no branch on the mode or provider.

The provider switch is observed through the gateway's two read-only scalars,
``provider_name`` and ``endpoint_host``. The gateway never hands out the provider object
(that would be a path past the gates; see the module docstring of
``bianque/llm/gateway.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import fields
from typing import Any, get_args

import httpx
import pytest

from bianque.config import get_settings
from bianque.config.settings import LLMMode
from bianque.llm import CallContext, LLMCompletion, LLMGateway, LLMUsage, build_gateway
from bianque.llm.providers import (
    PROVIDER_REGISTRY,
    ProviderOptions,
    ScriptedProvider,
    build_provider,
)
from bianque.llm.redacted import redact_prompt_for_llm
from bianque.llm.request import ModelRequest


class _Recorder:
    """Record every request sent through ``MockTransport`` (count and raw bytes)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    @property
    def count(self) -> int:
        return len(self.requests)


_COMPLETION_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
}


def _make_client(recorder: _Recorder) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        recorder.bodies.append(request.content)
        return httpx.Response(200, json=_COMPLETION_RESPONSE)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(main())


#: The calling code: it depends only on LLMGateway and has no branch on the mode.
#: Switching configuration does not change a single byte of this function.
async def _call_sequence(gateway: LLMGateway) -> LLMCompletion:
    return await gateway.complete(
        redact_prompt_for_llm([("user", "why did pod restart")]),
        context=CallContext(agent="investigation", operation="plan"),
    )


def _hosted_api_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted API: live mode with the SDK default endpoint."""
    monkeypatch.setenv("BIANQUE_LLM_MODE", "live")
    monkeypatch.delenv("BIANQUE_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("BIANQUE_LLM_MODEL", "test-model")


def _local_openai_compatible_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local OpenAI-compatible endpoint (vLLM or Ollama look the same)."""
    monkeypatch.setenv("BIANQUE_LLM_MODE", "live")
    monkeypatch.setenv("BIANQUE_LLM_BASE_URL", "http://llm.example.com:8000/v1")
    monkeypatch.setenv("BIANQUE_LLM_MODEL", "test-model")


# Switching the mode is a configuration change only.
def test_provider_switch_by_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setenv("BIANQUE_LLM_MODE", "scripted")
    monkeypatch.delenv("BIANQUE_LLM_BASE_URL", raising=False)

    async def with_scripted() -> LLMCompletion:
        gateway = build_gateway(get_settings(), http_client=_make_client(recorder))
        # Observed through the gateway's two read-only scalars. The provider object is
        # not public (with it a caller could bypass the gates), so the evidence of the
        # switch is the provider name and the endpoint host.
        assert gateway.provider_name == "scripted"
        assert gateway.endpoint_host is None
        return await _call_sequence(gateway)

    scripted_result = _run(with_scripted)

    _local_openai_compatible_config(monkeypatch)

    async def with_live() -> LLMCompletion:
        gateway = build_gateway(get_settings(), http_client=_make_client(recorder))
        assert gateway.provider_name == "openai-compat"
        assert gateway.endpoint_host == "llm.example.com"
        return await _call_sequence(gateway)

    live_result = _run(with_live)

    # The same calling function ran under both configurations.
    assert scripted_result.text != live_result.text
    assert isinstance(scripted_result, LLMCompletion)
    assert isinstance(live_result, LLMCompletion)
    # Scripted mode sends nothing; live mode sends one request. The difference lives
    # only in the provider layer.
    assert recorder.count == 1


# Two live endpoint configurations run the same call sequence with equivalent results.
def test_two_provider_configs_same_call_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    results: list[LLMCompletion] = []
    recorders: list[_Recorder] = []

    for configure in (_hosted_api_config, _local_openai_compatible_config):
        configure(monkeypatch)
        recorder = _Recorder()
        recorders.append(recorder)

        async def main(rec: _Recorder = recorder) -> LLMCompletion:
            gateway = build_gateway(get_settings(), http_client=_make_client(rec))
            return await _call_sequence(gateway)

        results.append(_run(main))

    first, second = results
    assert {f.name for f in fields(first)} == {f.name for f in fields(second)}
    usage_fields = {f.name for f in fields(LLMUsage)}
    assert usage_fields == {"prompt_tokens", "completion_tokens", "estimated"}
    for completion in results:
        assert {f.name for f in fields(completion.usage)} == usage_fields
    # Each configuration sends one request, to its own configured host.
    assert [rec.count for rec in recorders] == [1, 1]
    assert recorders[0].requests[0].url.host == "api.openai.com"
    assert recorders[1].requests[0].url.host == "llm.example.com"


# Scripted mode goes through the same interface, deterministically and offline.
def test_scripted_provider_deterministic_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_LLM_MODE", "scripted")
    monkeypatch.delenv("BIANQUE_LLM_BASE_URL", raising=False)
    recorder = _Recorder()

    async def main() -> tuple[LLMCompletion, LLMCompletion]:
        provider = build_provider(
            get_settings(), ProviderOptions(http_client=_make_client(recorder))
        )
        assert isinstance(provider, ScriptedProvider)
        request = ModelRequest(
            agent="investigation",
            operation="plan",
            prompt=redact_prompt_for_llm([("user", "why did pod restart")]),
            model="",
        )
        return await provider.complete(request), await provider.complete(request)

    first_completion, second_completion = _run(main)

    assert first_completion == second_completion, "the scripted completion is not deterministic"
    assert recorder.count == 0, "the scripted provider sent a request"


# Every mode the settings accept has a registered provider.
def test_every_configurable_mode_has_a_provider() -> None:
    assert set(PROVIDER_REGISTRY) == set(get_args(LLMMode))


# A mode without a registered provider fails when the gateway is built, naming the
# available modes. Settings reject unknown modes, so the registry entry is removed here.
def test_unregistered_mode_fails_with_available_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_LLM_MODE", "recorded")
    monkeypatch.delitem(PROVIDER_REGISTRY, "recorded")
    with pytest.raises(ValueError, match="recorded") as excinfo:
        build_gateway(get_settings())
    message = str(excinfo.value)
    assert "'live'" in message
    assert "'scripted'" in message
