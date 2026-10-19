"""``scripted`` mode: rule-based answers without any model or network.

- The built-in fallback rule answers a forced function call with arguments that
  validate against the function's schema, for a model with nested objects, a list with a
  minimum length, an enum, a ``Literal``, bounded numbers and an optional field.
- Injected rules run before the fallback, in order; a rule returning ``None`` passes.
- Without a forced function the fallback answers with empty text and no call.
- Building a scripted gateway logs a ``WARNING`` that names the mode.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

import pytest
from llm_fakes import CONTEXT, FakeEndpoint, prompt, settings_from_env
from pydantic import BaseModel, Field

from bianque.llm import (
    LLMCompletion,
    LLMGateway,
    ScriptedReply,
    ScriptedRule,
    ToolCall,
    ToolSpec,
    build_gateway,
)
from bianque.llm.request import ModelRequest
from bianque.llm.scripted import minimal_instance


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class Citation(BaseModel):
    """One piece of evidence a finding rests on."""

    evidence_id: str = Field(min_length=4)
    weight: int = Field(ge=1, le=10)


class Workload(BaseModel):
    namespace: str
    name: str = Field(min_length=1)
    replicas: int = Field(gt=0)


class Finding(BaseModel):
    """A root cause finding with its evidence."""

    severity: Severity
    kind: Literal["oom", "crash_loop", "image_pull"]
    workload: Workload
    citations: list[Citation] = Field(min_length=2)
    confidence: float = Field(ge=0.0, le=1.0)
    restarts: int = Field(ge=3)
    summary: str = Field(min_length=10, max_length=200)
    suspected_change: str | None = None
    is_recurring: bool


_REPORT = ToolSpec("report_finding", "Report the root cause.", Finding.model_json_schema())
_QUERY_LOGS = ToolSpec(
    "query_logs",
    "Search the logs of a pod.",
    {"type": "object", "properties": {"pod": {"type": "string"}}, "required": ["pod"]},
)


def _gateway(
    monkeypatch: pytest.MonkeyPatch,
    rules: tuple[ScriptedRule, ...] = (),
    endpoint: FakeEndpoint | None = None,
) -> LLMGateway:
    settings = settings_from_env(monkeypatch, {"BIANQUE_LLM_MODE": "scripted"})
    return build_gateway(
        settings, scripted_rules=rules, http_client=endpoint.client() if endpoint else None
    )


async def _ask(
    gateway: LLMGateway, tools: tuple[ToolSpec, ...] = (), tool_choice: str = "auto"
) -> LLMCompletion:
    return await gateway.complete(
        prompt(("user", "why did checkout restart")),
        context=CONTEXT,
        tools=tools,
        tool_choice=tool_choice,
    )


# -- Fallback rule -------------------------------------------------------------------


@pytest.mark.parametrize("tool_choice", ["report_finding", "required"])
async def test_fallback_arguments_validate_against_the_forced_schema(
    monkeypatch: pytest.MonkeyPatch, tool_choice: str
) -> None:
    endpoint = FakeEndpoint()
    gateway = _gateway(monkeypatch, endpoint=endpoint)
    assert gateway.provider_name == "scripted"
    assert gateway.endpoint_host is None

    completion = await _ask(gateway, tools=(_REPORT,), tool_choice=tool_choice)

    [call] = completion.tool_calls
    assert call.name == "report_finding"
    finding = Finding.model_validate_json(call.arguments)
    assert len(finding.citations) == 2
    assert finding.suspected_change is None
    assert completion.finish_reason == "tool_calls"
    assert completion.text == ""
    assert completion.model == "scripted"
    assert completion.usage.estimated is True
    assert endpoint.count == 0, "scripted mode sent a request"


async def test_fallback_calls_the_named_function_among_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(monkeypatch)

    completion = await _ask(gateway, tools=(_QUERY_LOGS, _REPORT), tool_choice="report_finding")

    [call] = completion.tool_calls
    assert call.name == "report_finding"
    Finding.model_validate_json(call.arguments)


@pytest.mark.parametrize(
    ("tools", "tool_choice"),
    [((), "auto"), ((_QUERY_LOGS, _REPORT), "auto")],
    ids=["no-tools", "tools-auto"],
)
async def test_fallback_without_forced_function_returns_empty_text(
    monkeypatch: pytest.MonkeyPatch, tools: tuple[ToolSpec, ...], tool_choice: str
) -> None:
    gateway = _gateway(monkeypatch)

    completion = await _ask(gateway, tools=tools, tool_choice=tool_choice)

    assert completion.text == ""
    assert completion.tool_calls == ()
    assert completion.finish_reason == "stop"


async def test_scripted_answers_are_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _gateway(monkeypatch)

    first = await _ask(gateway, tools=(_REPORT,), tool_choice="report_finding")
    second = await _ask(gateway, tools=(_REPORT,), tool_choice="report_finding")

    assert first == second


# -- Injected rules ------------------------------------------------------------------


async def test_injected_rule_takes_precedence_over_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ModelRequest] = []

    def rule(request: ModelRequest) -> ScriptedReply | None:
        seen.append(request)
        return ScriptedReply(
            text="the logs first", tool_calls=(("query_logs", {"pod": "checkout-7"}),)
        )

    gateway = _gateway(monkeypatch, rules=(rule,))

    # The fallback would have called report_finding; the injected rule wins.
    completion = await _ask(gateway, tools=(_QUERY_LOGS, _REPORT), tool_choice="report_finding")

    assert completion.text == "the logs first"
    assert completion.tool_calls == (
        ToolCall("scripted-0", "query_logs", json.dumps({"pod": "checkout-7"})),
    )
    [request] = seen
    assert request.agent == "investigation"
    assert request.tool_choice == "report_finding"
    assert [tool.name for tool in request.tools] == ["query_logs", "report_finding"]


async def test_rules_run_in_order_and_none_passes_to_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def declines(request: ModelRequest) -> ScriptedReply | None:
        calls.append("declines")
        return None

    def answers(request: ModelRequest) -> ScriptedReply | None:
        calls.append("answers")
        return ScriptedReply(text="second rule")

    def never_reached(request: ModelRequest) -> ScriptedReply | None:
        calls.append("never_reached")
        return ScriptedReply(text="third rule")

    gateway = _gateway(monkeypatch, rules=(declines, answers, never_reached))

    completion = await _ask(gateway)

    assert completion.text == "second rule"
    assert calls == ["declines", "answers"]


async def test_declining_rules_fall_through_to_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(monkeypatch, rules=(lambda request: None,))

    completion = await _ask(gateway, tools=(_REPORT,), tool_choice="report_finding")

    [call] = completion.tool_calls
    Finding.model_validate_json(call.arguments)


# -- Warning ------------------------------------------------------------------------


def test_building_a_scripted_gateway_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _gateway(monkeypatch)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "scripted" in warnings[0].getMessage()


@pytest.mark.parametrize("mode", ["live", "recorded"])
def test_other_modes_log_no_scripted_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    settings = settings_from_env(
        monkeypatch, {"BIANQUE_LLM_MODE": mode, "BIANQUE_LLM_MODEL": "test-model"}
    )
    with caplog.at_level(logging.WARNING):
        build_gateway(settings)

    assert not [record for record in caplog.records if "scripted" in record.getMessage()]


class _FormatsAndBounds(BaseModel):
    at: datetime
    day: date
    uid: UUID
    ratio: float = Field(gt=0.5, le=1)
    below: float = Field(lt=-2)
    negative: int = Field(le=-1)
    above: int = Field(gt=3)
    window: int = Field(ge=10, le=20)


def test_minimal_instance_covers_formats_and_bounds() -> None:
    value = minimal_instance(_FormatsAndBounds.model_json_schema())
    _FormatsAndBounds.model_validate(value)
