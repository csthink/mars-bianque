"""Switching the model mode is a configuration change only.

One calling function, ``_diagnose``, runs in ``live``, ``recorded`` and ``scripted``
mode. The three configurations share every environment variable except
``BIANQUE_LLM_MODE``: the record and recordings directories point at the same folder in
all three, so ``live`` writes the recording that ``recorded`` then replays.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from llm_fakes import BASE_URL, CONTEXT, MODEL, FakeEndpoint, ok, prompt, settings_from_env
from pydantic import BaseModel, Field

from bianque.llm import LLMCompletion, LLMGateway, ToolCall, ToolSpec, build_gateway


class RootCause(BaseModel):
    """The root cause of an alert."""

    component: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


_REPORT = ToolSpec("report_root_cause", "Report the root cause.", RootCause.model_json_schema())


#: The calling code: it depends only on LLMGateway and has no branch on the mode.
async def _diagnose(gateway: LLMGateway) -> LLMCompletion:
    return await gateway.complete(
        prompt(("system", "you are an SRE assistant"), ("user", "why did checkout restart")),
        context=CONTEXT,
        tools=(_REPORT,),
        tool_choice="report_root_cause",
    )


def _env(mode: str, directory: Path) -> dict[str, str | Path]:
    return {
        "BIANQUE_LLM_MODE": mode,
        "BIANQUE_LLM_BASE_URL": BASE_URL,
        "BIANQUE_LLM_MODEL": MODEL,
        "BIANQUE_LLM_EXTRA_BODY": '{"thinking": {"type": "disabled"}}',
        "BIANQUE_LLM_RECORD_DIR": directory,
        "BIANQUE_LLM_RECORDINGS_DIR": directory,
    }


async def test_same_calling_code_runs_in_all_three_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_call = ToolCall(
        "call-1", "report_root_cause", '{"component": "checkout", "evidence_ids": ["ev-1"]}'
    )
    endpoint = FakeEndpoint(ok(text=None, tool_calls=(live_call,), usage=(200, 20)))
    results: dict[str, LLMCompletion] = {}
    providers: dict[str, tuple[str, str | None]] = {}

    for mode in ("live", "recorded", "scripted"):
        settings = settings_from_env(monkeypatch, _env(mode, tmp_path))
        gateway = build_gateway(settings, http_client=endpoint.client())
        providers[mode] = (gateway.provider_name, gateway.endpoint_host)
        results[mode] = await _diagnose(gateway)

    assert providers == {
        "live": ("openai-compat", "llm.example.com"),
        "recorded": ("recorded", None),
        "scripted": ("scripted", None),
    }
    # Only live mode reaches the endpoint.
    assert endpoint.count == 1
    # recorded mode returns what live mode recorded.
    assert results["recorded"] == results["live"]
    # Every mode answers the forced call with arguments the caller can use.
    for mode, completion in results.items():
        [call] = completion.tool_calls
        assert call.name == "report_root_cause", mode
        RootCause.model_validate_json(call.arguments)
