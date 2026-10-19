"""Gateway calls: structured output with one correction, function parameters, call records.

Structured output: the output model's schema is declared as one
function and forced. Missing, non-JSON or invalid arguments are shown back to the model
as its own call plus a tool message naming the validation errors, and the model gets
one more attempt; a second failure raises ``StructuredOutputError``. Each attempt is one
model call with its own call record.

Function parameters: the declared parameters are the output model's JSON Schema without
the top-level ``title`` and ``description``; a model read a top-level ``description``
(the pydantic docstring) as a parameter and nested every argument under it.

Call records: every ``complete()`` produces one ``CallEvent``, on
success and on failure, with the fields of ``call_records``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from llm_fakes import (
    CONTEXT,
    FakeEndpoint,
    SleepLog,
    live_env,
    ok,
    prompt,
    settings_from_env,
    status,
)
from pydantic import BaseModel, Field

from bianque.llm import (
    CallContext,
    LLMGateway,
    LLMRequestError,
    MemoryCallRecorder,
    RedactedPrompt,
    ScriptedReply,
    ScriptedRule,
    StructuredOutputError,
    ToolCall,
    ToolSpec,
    assistant_message,
    build_gateway,
)
from bianque.llm.gateway import function_parameters
from bianque.llm.request import ModelRequest


class Step(BaseModel):
    """One investigation step: a tool and why it is called."""

    tool: str = Field(min_length=1)
    reason: str


class Plan(BaseModel):
    """An investigation plan: a hypothesis and the steps that test it."""

    hypothesis: str = Field(min_length=5)
    steps: list[Step] = Field(min_length=1)


_FUNCTION = "make_plan"
_DESCRIPTION = "Return the investigation plan."
_VALID: dict[str, Any] = {
    "hypothesis": "checkout ran out of memory",
    "steps": [{"tool": "query_logs", "reason": "look for OOMKilled"}],
}
_INVALID: dict[str, Any] = {"steps": []}


def _question() -> RedactedPrompt:
    return prompt(("system", "you are an SRE assistant"), ("user", "why did checkout restart"))


def _scripted(
    monkeypatch: pytest.MonkeyPatch,
    rules: tuple[ScriptedRule, ...],
    recorder: MemoryCallRecorder | None = None,
) -> LLMGateway:
    settings = settings_from_env(monkeypatch, {"BIANQUE_LLM_MODE": "scripted"})
    return build_gateway(settings, recorder=recorder, scripted_rules=rules)


def _live(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: FakeEndpoint,
    recorder: MemoryCallRecorder | None = None,
) -> LLMGateway:
    settings = settings_from_env(monkeypatch, live_env())
    return build_gateway(
        settings, recorder=recorder, http_client=endpoint.client(), sleep=SleepLog()
    )


def _is_correction(request: ModelRequest) -> bool:
    """Whether the request already carries the tool message of a rejected attempt."""
    return any(message.role == "tool" for message in request.prompt.messages)


# -- Structured output -----------------------------------------------------------------


async def test_structured_output_valid_on_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = MemoryCallRecorder()
    # No injected rule: the fallback rule answers the forced function with minimal
    # arguments that satisfy the schema.
    gateway = _scripted(monkeypatch, (), recorder)

    result = await gateway.complete_structured(
        _question(), context=CONTEXT, output=Plan, function_name=_FUNCTION, description=_DESCRIPTION
    )

    assert isinstance(result.value, Plan)
    assert result.attempts == 1
    assert len(recorder.events) == 1


async def test_structured_output_corrected_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ModelRequest] = []

    def invalid_then_valid(request: ModelRequest) -> ScriptedReply | None:
        seen.append(request)
        arguments = _VALID if _is_correction(request) else _INVALID
        return ScriptedReply(text="drafting", tool_calls=((_FUNCTION, arguments),))

    recorder = MemoryCallRecorder()
    gateway = _scripted(monkeypatch, (invalid_then_valid,), recorder)
    original = _question()

    result = await gateway.complete_structured(
        original, context=CONTEXT, output=Plan, function_name=_FUNCTION, description=_DESCRIPTION
    )

    assert result.value == Plan.model_validate(_VALID)
    assert result.attempts == 2
    assert json.loads(result.completion.tool_calls[0].arguments) == _VALID
    assert [event.status for event in recorder.events] == ["ok", "ok"]

    first, second = seen
    # The output schema is the only function, and it is forced.
    assert first.tools == (ToolSpec(_FUNCTION, _DESCRIPTION, function_parameters(Plan)),)
    assert first.tool_choice == _FUNCTION
    assert first.prompt == original
    assert second.tools == first.tools
    assert second.tool_choice == _FUNCTION
    # The correction request is the original conversation plus the rejected call and a
    # tool message that names the validation errors.
    assert second.prompt.messages[: len(original.messages)] == original.messages
    assistant, tool = second.prompt.messages[len(original.messages) :]
    assert assistant.role == "assistant"
    assert assistant.content.text == "drafting"
    [rejected] = assistant.tool_calls
    assert rejected.name == _FUNCTION
    assert json.loads(rejected.arguments.text) == _INVALID
    assert tool.role == "tool"
    assert tool.tool_call_id == rejected.id
    assert "hypothesis: Field required" in tool.content.text
    assert "steps: List should have at least 1 item" in tool.content.text
    assert _FUNCTION in tool.content.text


async def test_structured_output_invalid_twice_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_invalid(request: ModelRequest) -> ScriptedReply | None:
        return ScriptedReply(tool_calls=((_FUNCTION, _INVALID),))

    recorder = MemoryCallRecorder()
    gateway = _scripted(monkeypatch, (always_invalid,), recorder)

    with pytest.raises(StructuredOutputError) as excinfo:
        await gateway.complete_structured(
            _question(),
            context=CONTEXT,
            output=Plan,
            function_name=_FUNCTION,
            description=_DESCRIPTION,
        )

    assert "plan" in str(excinfo.value)
    assert "hypothesis" in str(excinfo.value)
    assert len(recorder.events) == 2


async def test_structured_output_without_any_call_twice_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ModelRequest] = []

    def never_calls(request: ModelRequest) -> ScriptedReply | None:
        seen.append(request)
        return ScriptedReply(text="I would rather explain in prose")

    recorder = MemoryCallRecorder()
    gateway = _scripted(monkeypatch, (never_calls,), recorder)

    with pytest.raises(StructuredOutputError, match=f"did not call {_FUNCTION}"):
        await gateway.complete_structured(
            _question(),
            context=CONTEXT,
            output=Plan,
            function_name=_FUNCTION,
            description=_DESCRIPTION,
        )

    assert len(recorder.events) == 2
    # With no call to show, a placeholder call keeps the tool message well-formed.
    assistant, tool = seen[1].prompt.messages[-2:]
    [placeholder] = assistant.tool_calls
    assert placeholder.name == _FUNCTION
    assert placeholder.arguments.text == "{}"
    assert tool.tool_call_id == placeholder.id
    assert f"did not call {_FUNCTION}" in tool.content.text


async def test_structured_output_missing_call_then_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    def prose_then_valid(request: ModelRequest) -> ScriptedReply | None:
        if _is_correction(request):
            return ScriptedReply(tool_calls=((_FUNCTION, _VALID),))
        return ScriptedReply(text="I would rather explain in prose")

    gateway = _scripted(monkeypatch, (prose_then_valid,))

    result = await gateway.complete_structured(
        _question(), context=CONTEXT, output=Plan, function_name=_FUNCTION, description=_DESCRIPTION
    )

    assert result.attempts == 2
    assert result.value == Plan.model_validate(_VALID)


async def test_structured_output_corrects_non_json_arguments_in_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint(
        ok(text=None, tool_calls=(ToolCall("call-1", _FUNCTION, "{not json"),)),
        ok(text=None, tool_calls=(ToolCall("call-2", _FUNCTION, json.dumps(_VALID)),)),
    )
    recorder = MemoryCallRecorder()
    gateway = _live(monkeypatch, endpoint, recorder)

    result = await gateway.complete_structured(
        _question(), context=CONTEXT, output=Plan, function_name=_FUNCTION, description=_DESCRIPTION
    )

    assert result.attempts == 2
    assert result.value == Plan.model_validate(_VALID)
    assert endpoint.count == 2
    assert len(recorder.events) == 2
    forced = {"type": "function", "function": {"name": _FUNCTION}}
    assert [body["tool_choice"] for body in endpoint.bodies] == [forced, forced]
    assistant, tool = endpoint.bodies[1]["messages"][-2:]
    assert assistant["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": _FUNCTION, "arguments": "{not json"},
        }
    ]
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "call-1"
    assert "(root): Invalid JSON" in tool["content"]


async def test_structured_output_propagates_model_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeEndpoint(status(400))
    gateway = _live(monkeypatch, endpoint)

    with pytest.raises(LLMRequestError):
        await gateway.complete_structured(
            _question(),
            context=CONTEXT,
            output=Plan,
            function_name=_FUNCTION,
            description=_DESCRIPTION,
        )

    assert endpoint.count == 1


# -- Function parameters ---------------------------------------------------------------


def test_function_parameters_drop_top_level_title_and_description() -> None:
    schema = Plan.model_json_schema()
    # The precondition: pydantic does put the class name and docstring at the top level.
    assert schema["title"] == "Plan"
    assert "investigation plan" in schema["description"]

    parameters = function_parameters(Plan)

    assert "title" not in parameters
    assert "description" not in parameters
    assert parameters == {
        key: value for key, value in schema.items() if key not in ("title", "description")
    }


async def test_declared_function_parameters_in_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint(
        ok(text=None, tool_calls=(ToolCall("call-1", _FUNCTION, json.dumps(_VALID)),))
    )
    gateway = _live(monkeypatch, endpoint)

    await gateway.complete_structured(
        _question(), context=CONTEXT, output=Plan, function_name=_FUNCTION, description=_DESCRIPTION
    )

    [tool] = endpoint.bodies[0]["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == _FUNCTION
    # The function carries the description; the parameters do not repeat the docstring.
    assert tool["function"]["description"] == _DESCRIPTION
    parameters = tool["function"]["parameters"]
    assert "title" not in parameters
    assert "description" not in parameters
    # Everything below the top level is kept, nested models' titles and docstrings too.
    schema = Plan.model_json_schema()
    assert parameters["type"] == "object"
    assert parameters["properties"] == schema["properties"]
    assert parameters["required"] == schema["required"]
    assert parameters["$defs"] == schema["$defs"]
    assert parameters["$defs"]["Step"]["title"] == "Step"
    assert "investigation step" in parameters["$defs"]["Step"]["description"]


# -- Call records ----------------------------------------------------------------------


async def test_successful_call_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(request: ModelRequest) -> ScriptedReply | None:
        time.sleep(0.02)
        return ScriptedReply(
            text="OOM in checkout", tool_calls=(("query_logs", {"pod": "checkout-7"}),)
        )

    recorder = MemoryCallRecorder()
    gateway = _scripted(monkeypatch, (answer,), recorder)
    conversation = _question() + (
        assistant_message("", [ToolCall("call-0", "get_events", '{"namespace": "shop"}')]),
    )
    before = datetime.now(UTC)

    completion = await gateway.complete(conversation, context=CONTEXT)

    after = datetime.now(UTC)
    [event] = recorder.events
    assert event.kind == "model"
    assert event.agent == "investigation"
    assert event.name == "plan"
    assert event.investigation_id == "inv-0001"
    assert event.session_id == 7
    assert event.status == "ok"
    assert event.error is None
    assert event.retries == 0
    assert before <= event.started_at <= after
    assert event.started_at.tzinfo is not None
    assert event.latency_ms >= 20
    assert event.tokens_in == completion.usage.prompt_tokens
    assert event.tokens_out == completion.usage.completion_tokens
    assert event.tokens_in is not None and event.tokens_in > 0
    assert "system: you are an SRE assistant" in event.input_summary
    assert "user: why did checkout restart" in event.input_summary
    assert 'assistant calls get_events: {"namespace": "shop"}' in event.input_summary
    assert event.output_summary is not None
    assert "OOM in checkout" in event.output_summary
    assert 'calls query_logs: {"pod": "checkout-7"}' in event.output_summary


async def test_failed_call_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = MemoryCallRecorder()
    gateway = _live(monkeypatch, FakeEndpoint(status(400)), recorder)
    context = CallContext("followup", "answer", investigation_id="inv-0002", session_id=3)

    with pytest.raises(LLMRequestError):
        await gateway.complete(_question(), context=context)

    [event] = recorder.events
    assert event.agent == "followup"
    assert event.name == "answer"
    assert event.investigation_id == "inv-0002"
    assert event.session_id == 3
    assert event.status == "error"
    assert event.error is not None
    assert event.error.startswith("LLMRequestError: ")
    assert "400" in event.error
    assert event.retries == 0
    assert event.latency_ms >= 0
    assert "user: why did checkout restart" in event.input_summary
    assert event.output_summary is None
    assert event.tokens_in is None
    assert event.tokens_out is None


async def test_one_event_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = MemoryCallRecorder()
    endpoint = FakeEndpoint(ok(), status(404), ok())
    gateway = _live(monkeypatch, endpoint, recorder)

    for operation in ("plan", "assess", "report"):
        try:
            await gateway.complete(
                _question(), context=CallContext("investigation", operation, "inv-0003")
            )
        except LLMRequestError:
            pass

    assert [(event.name, event.status) for event in recorder.events] == [
        ("plan", "ok"),
        ("assess", "error"),
        ("report", "ok"),
    ]
    assert all(event.latency_ms >= 0 for event in recorder.events)
