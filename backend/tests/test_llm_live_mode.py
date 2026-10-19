"""``live`` mode against an OpenAI-compatible endpoint behind ``httpx.MockTransport``.

Checked from the raw request body and the parsed completion:

- the request carries the tools in OpenAI function format, ``tool_choice`` (``auto``,
  ``required`` or a named function), ``temperature`` 0 and the ``BIANQUE_LLM_EXTRA_BODY``
  fields at the top level;
- an assistant message with tool calls and a tool message with ``tool_call_id`` are
  serialized in OpenAI format;
- tool calls in the response become ``ToolCall`` values; missing usage is estimated;
- error classification: timeouts, connection failures, HTTP 429 and 5xx are retried with
  delays 1, 2, 4 seconds up to ``BIANQUE_LLM_MAX_RETRIES``; other 4xx are not retried.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
import pytest
from llm_fakes import (
    CONTEXT,
    MODEL,
    FakeEndpoint,
    Reply,
    SleepLog,
    live_env,
    ok,
    prompt,
    settings_from_env,
    status,
)

from bianque.llm import (
    LLMCompletion,
    LLMGateway,
    LLMRequestError,
    LLMUnavailableError,
    LLMUsage,
    MemoryCallRecorder,
    RedactedPrompt,
    ToolCall,
    ToolSpec,
    assistant_message,
    build_gateway,
    redact_for_llm,
    tool_result_message,
)
from bianque.llm.redacted import RedactedMessage

_QUERY_LOGS = ToolSpec(
    name="query_logs",
    description="Search the logs of a pod.",
    parameters={
        "type": "object",
        "properties": {"pod": {"type": "string"}, "since_minutes": {"type": "integer"}},
        "required": ["pod"],
    },
)
_GET_EVENTS = ToolSpec(
    name="get_events",
    description="List the events of a namespace.",
    parameters={"type": "object", "properties": {"namespace": {"type": "string"}}},
)


def _gateway(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: FakeEndpoint,
    *,
    recorder: MemoryCallRecorder | None = None,
    sleep: SleepLog | None = None,
    **env: str,
) -> LLMGateway:
    settings = settings_from_env(monkeypatch, live_env(**env))
    return build_gateway(
        settings, recorder=recorder, http_client=endpoint.client(), sleep=sleep or SleepLog()
    )


async def _ask(gateway: LLMGateway, **kwargs: Any) -> LLMCompletion:
    return await gateway.complete(
        prompt(("system", "you are an SRE assistant"), ("user", "why did checkout restart")),
        context=CONTEXT,
        **kwargs,
    )


# -- Request body -------------------------------------------------------------------


async def test_request_body_carries_tools_temperature_and_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint()
    gateway = _gateway(
        monkeypatch,
        endpoint,
        BIANQUE_LLM_EXTRA_BODY='{"thinking": {"type": "disabled"}, "top_k": 1}',
    )

    await _ask(gateway, tools=(_QUERY_LOGS, _GET_EVENTS))

    assert endpoint.count == 1
    body = endpoint.bodies[0]
    assert body["model"] == MODEL
    assert body["temperature"] == 0
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "query_logs",
                "description": "Search the logs of a pod.",
                "parameters": _QUERY_LOGS.parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_events",
                "description": "List the events of a namespace.",
                "parameters": _GET_EVENTS.parameters,
            },
        },
    ]
    assert body["tool_choice"] == "auto"
    # Extra body fields are merged at the top level, not nested under another key.
    assert body["thinking"] == {"type": "disabled"}
    assert body["top_k"] == 1
    assert "extra_body" not in body
    assert body["messages"] == [
        {"role": "system", "content": "you are an SRE assistant"},
        {"role": "user", "content": "why did checkout restart"},
    ]


@pytest.mark.parametrize(
    ("tool_choice", "sent"),
    [
        ("auto", "auto"),
        ("required", "required"),
        ("query_logs", {"type": "function", "function": {"name": "query_logs"}}),
    ],
)
async def test_tool_choice_is_sent_in_openai_format(
    monkeypatch: pytest.MonkeyPatch, tool_choice: str, sent: object
) -> None:
    endpoint = FakeEndpoint()
    gateway = _gateway(monkeypatch, endpoint)

    await _ask(gateway, tools=(_QUERY_LOGS, _GET_EVENTS), tool_choice=tool_choice)

    assert endpoint.bodies[0]["tool_choice"] == sent


async def test_request_without_tools_or_extra_body_sends_neither(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint()
    gateway = _gateway(monkeypatch, endpoint)

    await _ask(gateway)

    body = endpoint.bodies[0]
    assert set(body) == {"model", "messages", "temperature"}


async def test_tool_turn_is_serialized_in_openai_format(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeEndpoint()
    gateway = _gateway(monkeypatch, endpoint)
    conversation = RedactedPrompt(
        [
            RedactedMessage("user", redact_for_llm("why did checkout restart")),
            assistant_message(
                "let me look at the logs",
                [
                    ToolCall("call-1", "query_logs", '{"pod": "checkout-7"}'),
                    ToolCall("call-2", "get_events", '{"namespace": "shop"}'),
                ],
            ),
            tool_result_message("call-1", "OOMKilled at 10:02"),
            tool_result_message("call-2", "BackOff restarting failed container"),
        ]
    )

    await gateway.complete(conversation, context=CONTEXT, tools=(_QUERY_LOGS, _GET_EVENTS))

    assert endpoint.bodies[0]["messages"] == [
        {"role": "user", "content": "why did checkout restart"},
        {
            "role": "assistant",
            "content": "let me look at the logs",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "query_logs", "arguments": '{"pod": "checkout-7"}'},
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "get_events", "arguments": '{"namespace": "shop"}'},
                },
            ],
        },
        {"role": "tool", "content": "OOMKilled at 10:02", "tool_call_id": "call-1"},
        {
            "role": "tool",
            "content": "BackOff restarting failed container",
            "tool_call_id": "call-2",
        },
    ]


# -- Response parsing ---------------------------------------------------------------


async def test_response_tool_calls_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = (
        ToolCall("call-a", "query_logs", '{"pod": "checkout-7", "since_minutes": 30}'),
        ToolCall("call-b", "get_events", '{"namespace": "shop"}'),
    )
    endpoint = FakeEndpoint(ok(text=None, tool_calls=calls, usage=(120, 40)))
    gateway = _gateway(monkeypatch, endpoint)

    completion = await _ask(gateway, tools=(_QUERY_LOGS, _GET_EVENTS))

    assert completion.tool_calls == calls
    assert completion.text == ""
    assert completion.finish_reason == "tool_calls"
    assert completion.model == MODEL
    assert completion.usage == LLMUsage(120, 40, estimated=False)


async def test_missing_usage_is_estimated(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = (ToolCall("call-a", "query_logs", '{"pod": "checkout-7"}'),)
    endpoint = FakeEndpoint(ok(text="checking logs", tool_calls=calls, usage=None))
    gateway = _gateway(monkeypatch, endpoint)

    completion = await _ask(gateway, tools=(_QUERY_LOGS,))

    # Characters divided by 4, rounded up: the prompt texts, and the output text plus the
    # tool call arguments.
    prompt_chars = len("you are an SRE assistant") + len("why did checkout restart")
    output_chars = len("checking logs") + len('{"pod": "checkout-7"}')
    assert completion.usage == LLMUsage(
        math.ceil(prompt_chars / 4), math.ceil(output_chars / 4), estimated=True
    )


# -- Error classification and retries ----------------------------------------------


def _timeout() -> httpx.TimeoutException:
    return httpx.ReadTimeout("read timed out")


def _connection_failure() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


@pytest.mark.parametrize(
    "failure",
    [status(429), status(500), status(502), status(503), _timeout(), _connection_failure()],
    ids=["429", "500", "502", "503", "timeout", "connection"],
)
async def test_retryable_failure_is_retried_until_success(
    monkeypatch: pytest.MonkeyPatch, failure: Reply
) -> None:
    endpoint = FakeEndpoint(failure, failure, ok(text="recovered"))
    recorder = MemoryCallRecorder()
    sleep = SleepLog()
    gateway = _gateway(monkeypatch, endpoint, recorder=recorder, sleep=sleep)

    completion = await _ask(gateway)

    assert completion.text == "recovered"
    assert endpoint.count == 3
    assert sleep.delays == [1.0, 2.0]
    [event] = recorder.events
    assert event.status == "ok"
    assert event.retries == 2


async def test_mixed_retryable_failures_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeEndpoint(status(429), status(500), ok(text="recovered"))
    recorder = MemoryCallRecorder()
    sleep = SleepLog()
    gateway = _gateway(monkeypatch, endpoint, recorder=recorder, sleep=sleep)

    completion = await _ask(gateway)

    assert completion.text == "recovered"
    assert sleep.delays == [1.0, 2.0]
    assert [event.retries for event in recorder.events] == [2]


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (status(500), "server_error"),
        (status(429), "rate_limited"),
        (_timeout(), "timeout"),
        (_connection_failure(), "connection"),
    ],
    ids=["500", "429", "timeout", "connection"],
)
async def test_persistent_retryable_failure_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch, failure: Reply, category: str
) -> None:
    endpoint = FakeEndpoint(failure)
    recorder = MemoryCallRecorder()
    sleep = SleepLog()
    gateway = _gateway(monkeypatch, endpoint, recorder=recorder, sleep=sleep)

    with pytest.raises(LLMUnavailableError) as excinfo:
        await _ask(gateway)

    # The default BIANQUE_LLM_MAX_RETRIES is 2: one attempt plus two retries.
    assert endpoint.count == 3
    assert excinfo.value.attempts == 3
    assert excinfo.value.category == category
    assert sleep.delays == [1.0, 2.0]
    [event] = recorder.events
    assert event.status == "error"
    assert event.retries == 2
    assert event.error is not None and "LLMUnavailableError" in event.error


@pytest.mark.parametrize(("max_retries", "delays"), [(0, []), (1, [1.0]), (3, [1.0, 2.0, 4.0])])
async def test_max_retries_setting_bounds_attempts(
    monkeypatch: pytest.MonkeyPatch, max_retries: int, delays: list[float]
) -> None:
    endpoint = FakeEndpoint(status(503))
    sleep = SleepLog()
    gateway = _gateway(monkeypatch, endpoint, sleep=sleep, BIANQUE_LLM_MAX_RETRIES=str(max_retries))

    with pytest.raises(LLMUnavailableError):
        await _ask(gateway)

    assert endpoint.count == max_retries + 1
    assert sleep.delays == delays


@pytest.mark.parametrize("code", [400, 401, 404, 422])
async def test_other_client_errors_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    endpoint = FakeEndpoint(status(code), ok())
    recorder = MemoryCallRecorder()
    sleep = SleepLog()
    gateway = _gateway(monkeypatch, endpoint, recorder=recorder, sleep=sleep)

    with pytest.raises(LLMRequestError, match=str(code)):
        await _ask(gateway)

    assert endpoint.count == 1
    assert sleep.delays == []
    [event] = recorder.events
    assert event.status == "error"
    assert event.retries == 0


async def test_response_without_choices_is_a_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": MODEL}
    endpoint = FakeEndpoint(httpx.Response(200, json={**body, "choices": []}))
    sleep = SleepLog()
    gateway = _gateway(monkeypatch, endpoint, sleep=sleep)

    with pytest.raises(LLMRequestError):
        await _ask(gateway)

    assert endpoint.count == 1
    assert sleep.delays == []


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(
            200, content=b"<html>proxy error</html>", headers={"content-type": "application/json"}
        ),
        httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL,
                "choices": [{"index": 0, "finish_reason": "stop"}],
            },
        ),
    ],
    ids=["not-json", "choice-without-message"],
)
async def test_unparseable_response_is_a_request_error(
    monkeypatch: pytest.MonkeyPatch, reply: httpx.Response
) -> None:
    endpoint = FakeEndpoint(reply)
    gateway = _gateway(monkeypatch, endpoint)

    with pytest.raises(LLMRequestError):
        await _ask(gateway)
    assert endpoint.count == 1
