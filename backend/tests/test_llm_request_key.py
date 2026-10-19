"""The request key that finds recorded responses.

- The same request gives the same key, also when rebuilt from scratch.
- Tool order and the key order of JSON tool arguments do not change it.
- Agent, message role or text, tool calls, tool schemas, model, temperature,
  ``tool_choice`` and the extra body fields each change it.
- The investigation and session IDs never reach it: they are not part of
  ``ModelRequest``, and two gateway calls that differ only in them produce one key.
- The key is the SHA-256 of the normalized request serialized as JSON with sorted keys
  and no extra whitespace.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest
from llm_fakes import prompt, settings_from_env

from bianque.llm import (
    CallContext,
    RedactedPrompt,
    ScriptedReply,
    ToolCall,
    ToolSpec,
    assistant_message,
    build_gateway,
    tool_result_message,
)
from bianque.llm.redacted import MessageRole
from bianque.llm.request import ModelRequest, normalize_request, request_key

_QUERY_LOGS = ToolSpec(
    "query_logs",
    "Search the logs of a pod.",
    {"type": "object", "properties": {"pod": {"type": "string"}}, "required": ["pod"]},
)
_GET_EVENTS = ToolSpec(
    "get_events",
    "List the events of a namespace.",
    {"type": "object", "properties": {"namespace": {"type": "string"}}},
)


def _conversation(
    *,
    question: str = "why did checkout restart",
    arguments: str = '{"pod": "checkout-7", "since_minutes": 30}',
    call_id: str = "call-1",
    result: str = "OOMKilled at 10:02",
    question_role: MessageRole = "user",
) -> RedactedPrompt:
    return prompt(("system", "you are an SRE assistant"), (question_role, question)) + (
        assistant_message("", [ToolCall(call_id, "query_logs", arguments)]),
        tool_result_message(call_id, result),
    )


def _request(**overrides: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "agent": "investigation",
        "operation": "plan",
        "prompt": _conversation(),
        "model": "test-model",
        "tools": (_QUERY_LOGS, _GET_EVENTS),
        "tool_choice": "auto",
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    values.update(overrides)
    return ModelRequest(**values)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prompts are redacted with the current settings; keep them free of the shell's.
    settings_from_env(monkeypatch)


def test_same_request_gives_same_key() -> None:
    first, second = _request(), _request()
    assert first is not second
    assert request_key(first) == request_key(second)
    assert request_key(first) == request_key(first)


def test_tool_order_does_not_change_key() -> None:
    assert request_key(_request(tools=(_QUERY_LOGS, _GET_EVENTS))) == request_key(
        _request(tools=(_GET_EVENTS, _QUERY_LOGS))
    )


def test_argument_key_order_and_whitespace_do_not_change_key() -> None:
    compact = _request(prompt=_conversation(arguments='{"pod":"checkout-7","since_minutes":30}'))
    reordered = _request(
        prompt=_conversation(arguments='{ "since_minutes": 30,  "pod": "checkout-7" }')
    )
    assert request_key(compact) == request_key(reordered)


def test_invalid_json_arguments_are_kept_verbatim() -> None:
    first = _request(prompt=_conversation(arguments="{not json"))
    second = _request(prompt=_conversation(arguments="{not json "))
    assert request_key(first) == request_key(_request(prompt=_conversation(arguments="{not json")))
    assert request_key(first) != request_key(second)


@pytest.mark.parametrize(
    "overrides",
    [
        lambda: {"agent": "kubernetes"},
        lambda: {"prompt": _conversation(question="why did payments restart")},
        lambda: {"prompt": _conversation(question_role="system")},
        lambda: {"prompt": _conversation(arguments='{"pod": "checkout-8", "since_minutes": 30}')},
        lambda: {"prompt": _conversation(call_id="call-2")},
        lambda: {"prompt": _conversation(result="Evicted at 10:02")},
        lambda: {"model": "other-model"},
        lambda: {"temperature": 0.7},
        lambda: {"tool_choice": "required"},
        lambda: {"tool_choice": "query_logs"},
        lambda: {"extra_body": {}},
        lambda: {"extra_body": {"thinking": {"type": "enabled"}}},
        lambda: {"tools": (_QUERY_LOGS,)},
        lambda: {
            "tools": (dataclasses.replace(_QUERY_LOGS, description="Search pod logs."), _GET_EVENTS)
        },
    ],
    ids=[
        "agent",
        "message-text",
        "message-role",
        "tool-call-arguments",
        "tool-call-id",
        "tool-result",
        "model",
        "temperature",
        "tool-choice-required",
        "tool-choice-named",
        "extra-body-empty",
        "extra-body-value",
        "tool-set",
        "tool-schema",
    ],
)
def test_each_key_input_changes_the_key(overrides: Callable[[], dict[str, Any]]) -> None:
    assert request_key(_request(**overrides())) != request_key(_request())


def test_key_is_sha256_of_canonical_json() -> None:
    request = _request()
    canonical = json.dumps(
        normalize_request(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    key = request_key(request)
    assert key == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(key) == 64


def test_normalized_request_keeps_only_key_inputs() -> None:
    normalized = normalize_request(
        _request(prompt=_conversation(arguments='{"since_minutes": 30, "pod": "checkout-7"}'))
    )
    assert normalized == {
        "agent": "investigation",
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "you are an SRE assistant"},
            {"role": "user", "content": "why did checkout restart"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "query_logs",
                        "arguments": '{"pod":"checkout-7","since_minutes":30}',
                    }
                ],
            },
            {"role": "tool", "content": "OOMKilled at 10:02", "tool_call_id": "call-1"},
        ],
        # Sorted by name.
        "tools": [
            {
                "name": "get_events",
                "description": "List the events of a namespace.",
                "parameters": _GET_EVENTS.parameters,
            },
            {
                "name": "query_logs",
                "description": "Search the logs of a pod.",
                "parameters": _QUERY_LOGS.parameters,
            },
        ],
        "sampling": {
            "temperature": 0.0,
            "tool_choice": "auto",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    }


def test_run_specific_ids_are_not_part_of_the_request() -> None:
    names = {field.name for field in dataclasses.fields(ModelRequest)}
    assert "investigation_id" not in names
    assert "session_id" not in names


async def test_gateway_requests_differing_only_in_ids_share_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ModelRequest] = []

    def capture(request: ModelRequest) -> ScriptedReply | None:
        seen.append(request)
        return ScriptedReply(text="ok")

    settings = settings_from_env(
        monkeypatch, {"BIANQUE_LLM_MODE": "scripted", "BIANQUE_LLM_MODEL": "test-model"}
    )
    gateway = build_gateway(settings, scripted_rules=(capture,))
    for context in (
        CallContext("investigation", "plan", investigation_id="inv-0001", session_id=1),
        CallContext("investigation", "plan", investigation_id="inv-0002", session_id=2),
        CallContext("investigation", "plan"),
    ):
        await gateway.complete(_conversation(), context=context, tools=(_QUERY_LOGS,))

    assert len(seen) == 3
    assert len({request_key(request) for request in seen}) == 1
    # The gateway fills the key inputs it owns from settings.
    assert seen[0].model == "test-model"
    assert seen[0].temperature == 0.0
