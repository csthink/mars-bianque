"""Behavior of the model gateway settings: internal domains, budget, timeout, credential.

Each setting is checked by its effect, not by its presence. A setting that nobody reads
passes both "the field exists" and "the default is right":

- The internal domain suffixes are probed in both directions: set, the host name is
  generalized; unset, it is left alone. A one-sided assertion cannot show that the
  setting is wired to the engine.
- The timeout is read back from the provider's own SDK client (``timeout_seconds``), not
  from ``Settings``, at three points (default and two overrides) to rule out a
  coincidental match. The client object itself is not public; see the module docstring
  of ``bianque/llm/providers/openai_compat.py``.
- The credential is absent from every representation yet still reaches the outgoing
  request, both as a literal value and as a ``file:`` reference.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from bianque.config import get_settings
from bianque.llm.providers import build_provider
from bianque.llm.providers.openai_compat import OpenAICompatProvider
from bianque.llm.redacted import redact_for_llm, redact_prompt_for_llm

#: Synthetic credentials (not real ones), used only to check that plaintext never
#: appears in any representation.
_SYNTHETIC_FILE_KEY = "bianque-test-synthetic-key-not-a-real-credential"
_SYNTHETIC_LITERAL_KEY = "bianque-test-literal-key-not-a-real-credential"


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


def _live_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_LLM_MODE", "live")
    monkeypatch.setenv("BIANQUE_LLM_BASE_URL", "http://llm.example.com:8000/v1")
    monkeypatch.setenv("BIANQUE_LLM_MODEL", "test-model")
    monkeypatch.delenv("BIANQUE_LLM_API_KEY", raising=False)


# Internal domain suffixes take effect, and the effect is observable in both directions.
def test_internal_domain_suffix_env_takes_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_REDACT_INTERNAL_DOMAINS", "internal.example,corp.example")
    redacted = redact_for_llm("service endpoint svc.corp.example is degraded")
    assert "svc.corp.example" not in redacted.text
    assert "*.internal" in redacted.text


def test_internal_domain_not_generalized_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIANQUE_REDACT_INTERNAL_DOMAINS", raising=False)
    redacted = redact_for_llm("service endpoint svc.corp.example is degraded")
    # Without suffixes nothing is generalized; otherwise the previous test could pass
    # trivially.
    assert redacted.text == "service endpoint svc.corp.example is degraded"


# The token budget is read through the prefix mapping, not a hand-written name.
def test_token_budget_env_takes_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_INVESTIGATION_TOKEN_BUDGET", "4096")
    assert get_settings().investigation_token_budget == 4096
    monkeypatch.setenv("BIANQUE_INVESTIGATION_TOKEN_BUDGET", "8192")
    assert get_settings().investigation_token_budget == 8192


# The timeout setting reaches the outgoing client, not just Settings.
def test_timeout_config_reaches_provider_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_config(monkeypatch)

    # (1) unset: the default 60; (2) and (3) two different overrides rule out a client
    # timeout that merely happens to equal some constant.
    monkeypatch.delenv("BIANQUE_LLM_TIMEOUT_SECONDS", raising=False)
    provider = build_provider(get_settings())
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.timeout_seconds == 60

    for override in (7, 13):
        monkeypatch.setenv("BIANQUE_LLM_TIMEOUT_SECONDS", str(override))
        provider = build_provider(get_settings())
        assert isinstance(provider, OpenAICompatProvider)
        assert provider.timeout_seconds == override, (
            f"configured timeout {override} did not reach the client "
            f"(got {provider.timeout_seconds!r})"
        )


# The credential never appears in plaintext, yet it is still sent.
def _assert_key_hidden_and_sent(key: str) -> None:
    settings = get_settings()
    recorder = _Recorder()
    provider = build_provider(settings, http_client=_make_client(recorder))

    for label, text in (
        ("repr(settings)", repr(settings)),
        ("str(settings)", str(settings)),
        ("settings.model_dump_json()", settings.model_dump_json()),
        ("repr(provider)", repr(provider)),
        ("str(provider)", str(provider)),
    ):
        assert key not in text, f"credential plaintext appears in {label}"

    async def main() -> None:
        await provider.complete(redact_prompt_for_llm([("user", "ping")]))

    _run(main)

    assert recorder.count == 1
    authorization = recorder.requests[0].headers.get("authorization", "")
    assert key in authorization, "masking lost the key: the request carries no credential"


def test_api_key_file_reference_absent_from_repr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "llm-key"
    key_file.write_text(_SYNTHETIC_FILE_KEY, encoding="utf-8")
    _live_config(monkeypatch)
    monkeypatch.setenv("BIANQUE_LLM_API_KEY", f"file:{key_file}")
    _assert_key_hidden_and_sent(_SYNTHETIC_FILE_KEY)


def test_api_key_literal_absent_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_config(monkeypatch)
    monkeypatch.setenv("BIANQUE_LLM_API_KEY", _SYNTHETIC_LITERAL_KEY)
    _assert_key_hidden_and_sent(_SYNTHETIC_LITERAL_KEY)
