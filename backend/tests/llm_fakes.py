"""Shared test doubles for the model gateway tests.

- ``settings_from_env`` builds ``Settings`` the way the process does, from ``BIANQUE_``
  environment variables only, after clearing every inherited one, so that a developer's
  shell cannot change a test's configuration.
- ``FakeEndpoint`` is an OpenAI-compatible endpoint behind ``httpx.MockTransport``: it
  answers from a queue of replies (the last one repeats) and keeps every request.
- ``SleepLog`` replaces the gateway's wait between retries: it records the delays and
  returns at once.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from bianque.config import ENV_PREFIX, Settings, get_settings
from bianque.llm import CallContext, RedactedPrompt, ToolCall, redact_prompt_for_llm
from bianque.llm.redacted import MessageRole

#: Endpoint and model of every ``live`` configuration in these tests.
BASE_URL = "http://llm.example.com:8000/v1"
MODEL = "test-model"

#: A call context with both run-specific IDs set.
CONTEXT = CallContext(
    agent="investigation", operation="plan", investigation_id="inv-0001", session_id=7
)

#: What ``FakeEndpoint`` answers with: an HTTP response, or an exception the transport
#: raises (such as ``httpx.TimeoutException``).
Reply = httpx.Response | Exception


def settings_from_env(
    monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str | Path] | None = None
) -> Settings:
    """Clear every ``BIANQUE_`` variable, set ``env``, and read the settings."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, str(value))
    return get_settings()


def live_env(**extra: str | Path) -> dict[str, str | Path]:
    """Environment of ``live`` mode against ``BASE_URL``, plus ``extra`` variables."""
    return {
        "BIANQUE_LLM_MODE": "live",
        "BIANQUE_LLM_BASE_URL": BASE_URL,
        "BIANQUE_LLM_MODEL": MODEL,
        **extra,
    }


def prompt(*messages: tuple[MessageRole, str]) -> RedactedPrompt:
    """Redact ``(role, text)`` pairs with the current settings."""
    return redact_prompt_for_llm(messages)


def completion_body(
    *,
    text: str | None = "ok",
    tool_calls: Sequence[ToolCall] = (),
    usage: tuple[int, int] | None = (11, 3),
    finish_reason: str | None = None,
    model: str = MODEL,
) -> dict[str, Any]:
    """An OpenAI-compatible chat completion response body."""
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in tool_calls
        ]
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
            }
        ],
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        }
    return body


def ok(**fields: Any) -> httpx.Response:
    """A 200 response carrying ``completion_body(**fields)``."""
    return httpx.Response(200, json=completion_body(**fields))


def status(code: int) -> httpx.Response:
    """An error response with the given HTTP status."""
    return httpx.Response(
        code, json={"error": {"message": f"status {code}", "type": "test", "code": None}}
    )


class FakeEndpoint:
    """An OpenAI-compatible endpoint behind ``httpx.MockTransport``."""

    def __init__(self, *replies: Reply) -> None:
        """Answer with ``replies`` in order; the last one repeats. Default: one ``ok()``."""
        self._replies: list[Reply] = list(replies) or [ok()]
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        # A fresh response per request, so a repeated reply can be read again.
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)

    def client(self) -> httpx.AsyncClient:
        """An HTTP client whose requests reach this endpoint and nothing else."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    @property
    def count(self) -> int:
        """Number of requests received."""
        return len(self.requests)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        """The JSON bodies of the requests received."""
        return [json.loads(request.content) for request in self.requests]


class SleepLog:
    """Stands in for ``asyncio.sleep``: records each delay and returns at once."""

    def __init__(self) -> None:
        """Start with no delays."""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """Record ``delay``."""
        self.delays.append(delay)
