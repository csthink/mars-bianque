"""Scripted investigations of the S1 evidence packs: plans, rounds, evidence and records."""

from __future__ import annotations

import re
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient
from investigation_harness import CASES, Investigated, database, investigate
from sqlalchemy import Engine, text

from bianque.agents.prompting import EvidenceView, evidence_header
from bianque.api.app import create_app
from bianque.cli import main
from bianque.config import Settings
from bianque.gate import OutputType, json_schema
from bianque.llm import ScriptedReply
from bianque.llm.request import ModelRequest
from bianque.storage.repositories import AssessmentRepository, PlanRepository

AGENTS = {"investigation", "kubernetes", "logs", "change"}


def _plan_json(result: Investigated, version: int) -> dict[str, Any]:
    plan = next(p for p in result.plans if p.version == version)
    return {
        "rationale": plan.rationale,
        "steps": [
            {
                "goal": s.goal,
                "domain": s.domain,
                "suggested_tools": s.suggested_tools,
                "expected_evidence": s.expected_evidence[0],
            }
            for s in result.steps
            if s.plan_id == plan.id
        ],
        "knowledge_refs": [],
    }


def _domains(result: Investigated, version: int) -> set[str]:
    plan = next(p for p in result.plans if p.version == version)
    return {s.domain for s in result.steps if s.plan_id == plan.id}


async def test_s1a_plans_first_then_collects_change_evidence_in_round_two(
    migrated: Engine, pg_url: str
) -> None:
    """S1-a replay: the plan precedes every tool call; round two adds the change domain."""
    result = await investigate(pg_url, "s1-a")

    assert result.investigation.status == "reported"
    first_tool_call = min(c.started_at for c in result.calls if c.kind == "tool")
    assert result.plans[0].created_at < first_tool_call
    jsonschema.validate(_plan_json(result, 1), json_schema(OutputType.PLAN))
    assert _domains(result, 1) == {"kubernetes", "logs"}

    assert len(result.plans) >= 2
    assert "change" in _domains(result, 2)
    first_assessment = result.assessments[0]
    assert not first_assessment.sufficient
    assert result.plans[1].created_at > first_assessment.created_at
    assert all(s.status == "done" for s in result.steps)


async def test_s1a_report_cites_the_commit_diff(migrated: Engine, pg_url: str) -> None:
    result = await investigate(pg_url, "s1-a")

    assert result.final is not None and result.final.status == "published"
    cause = result.final.body["root_cause"]
    assert cause["status"] == "identified" and cause["category"] == "config_error"
    assert cause["object"]["name"] == "s1-app"
    diff = next(e for e in result.evidence if e.tool == "change_show_commit")
    assert diff.id in cause["citations"]
    assert "value: invalid" in diff.redacted_content


async def test_plans_steps_and_assessments_read_back(migrated: Engine, pg_url: str) -> None:
    """Every plan version, step and assessment can be read by investigation."""
    result = await investigate(pg_url, "s1-a")
    inv = result.investigation.id
    async with database(pg_url) as sessions, sessions() as session:
        plans = await PlanRepository(session).list_plans(inv)
        steps = await PlanRepository(session).list_steps(inv)
        assessments = await AssessmentRepository(session).list(inv)
    assert [p.version for p in plans] == [1, 2]
    # Plan 2 had the skill loaded for plan 1 in its prompt.
    assert [p.skills for p in plans] == [["investigate-pod-restarts"]] * 2
    assert len(steps) == len(result.steps) and all(s.findings for s in steps)
    assert [a.round for a in assessments] == [1, 2]

    with TestClient(create_app(Settings(database_url=pg_url))) as client:
        detail = client.get(f"/api/investigations/{inv}").json()
    assert [p["version"] for p in detail["plans"]] == [1, 2]
    assert detail["plans"][0]["skills"] == ["investigate-pod-restarts"]
    assert sum(len(p["steps"]) for p in detail["plans"]) == len(steps)
    assert [a["round"] for a in detail["assessments"]] == [1, 2]
    assert detail["report"]["status"] == "published"


async def test_evidence_comes_from_the_alert_and_tools_only(migrated: Engine, pg_url: str) -> None:
    """The alert is evidence; no evidence comes from a model or an agent."""
    result = await investigate(pg_url, "s1-a")

    assert {e.source for e in result.evidence} <= {"alertmanager", "kubernetes", "git"}
    alert = [e for e in result.evidence if e.source == "alertmanager"]
    assert len(alert) == 1 and alert[0].tool is None
    assert "BianquePodRestarting" in alert[0].raw_content
    assert all(e.tool for e in result.evidence if e.source != "alertmanager")


async def test_call_records_count_every_call_and_name_their_agent(
    migrated: Engine, pg_url: str
) -> None:
    """One record per model call and per tool call, fields filled in."""
    requests: list[ModelRequest] = []

    def count(request: ModelRequest) -> ScriptedReply | None:
        requests.append(request)
        return None

    result = await investigate(pg_url, "s1-a", rules=[count])

    model_calls = [c for c in result.calls if c.kind == "model"]
    tool_calls = [c for c in result.calls if c.kind == "tool"]
    knowledge_calls = [c for c in result.calls if c.kind == "knowledge"]
    skill_calls = [c for c in result.calls if c.kind == "skill"]
    assert len(model_calls) == len(requests)
    assert [(c.agent, c.name) for c in knowledge_calls] == [("investigation", "retrieve_knowledge")]
    assert [(c.agent, c.name) for c in skill_calls] == [
        ("investigation", "investigate-pod-restarts")
    ]
    assert len(result.calls) == len(model_calls) + len(tool_calls) + 2
    requested_tools = [
        line
        for c in model_calls
        for line in (c.output_summary or "").splitlines()
        if line.startswith("calls ")
        and not line.startswith(("calls submit_", "calls activate_skill"))
    ]
    assert len(tool_calls) == len(requested_tools)
    assert {c.agent for c in result.calls} == AGENTS
    for call in result.calls:
        assert call.name and call.input_summary and call.status == "ok"
        assert call.started_at is not None and call.latency_ms is not None
    for call in model_calls:
        assert call.tokens_in and call.tokens_out
    assert knowledge_calls[0].tokens_in is None and knowledge_calls[0].output_summary
    [skill] = skill_calls
    assert skill.input_summary == "investigate-pod-restarts" and skill.tokens_in is None
    assert re.fullmatch(r"sha256 [0-9a-f]{12}; first load", skill.output_summary or "")
    # The plan's model turn that loaded the skill carries no gate result; every other
    # structured output passed the gate.
    activating = [c for c in model_calls if "calls activate_skill" in (c.output_summary or "")]
    assert [c.name for c in activating] == ["plan"] and activating[0].gate_result is None
    structured = [c for c in model_calls if c.name != "evidence_agent" and c not in activating]
    assert all(c.gate_result and c.gate_result["passed"] for c in structured)
    assert result.investigation.tokens_used == sum(
        (c.tokens_in or 0) + (c.tokens_out or 0) for c in model_calls
    )


async def test_s1d_log_timeout_is_missing_evidence(migrated: Engine, pg_url: str) -> None:
    """The logs tool times out; the investigation still reports, without high confidence."""
    result = await investigate(pg_url, "s1-d")

    assert result.investigation.status == "reported"
    assert result.final is not None
    report = result.final.body
    assert any("logs_get_container_logs" in m["what"] for m in report["missing_evidence"])
    assert report["root_cause"]["confidence"] != "high"
    log_calls = [c for c in result.calls if c.name == "logs_get_container_logs"]
    assert log_calls and all(c.status == "error" and c.retries == 2 for c in log_calls)
    assert all(c.error and c.error.startswith("timeout") for c in log_calls)


async def test_s1f_without_evidence_the_root_cause_is_unknown(
    migrated: Engine, pg_url: str
) -> None:
    result = await investigate(pg_url, "s1-f")

    assert result.final is not None
    cause = result.final.body["root_cause"]
    assert cause["status"] == "unknown" and cause["confidence"] == "low"
    assert result.final.body["missing_evidence"]


@pytest.mark.parametrize("case", ["s1-b", "s1-c", "s1-e"])
async def test_other_cases_report(migrated: Engine, pg_url: str, case: str) -> None:
    result = await investigate(pg_url, case)
    assert result.investigation.status == "reported"
    assert result.final is not None and result.final.status == "published"


async def test_evidence_headers_survive_redaction(migrated: Engine, pg_url: str) -> None:
    """Every evidence entry of every prompt keeps its header, so tool names stay readable."""
    headers = re.compile(r"^\[ev_[0-9a-f]{10}\] source: \S+; tool: \S+$", re.M)
    entries = re.compile(r"^\[ev_", re.M)
    counts: list[tuple[int, int]] = []

    def check(request: ModelRequest) -> ScriptedReply | None:
        for message in request.prompt.messages:
            if message.role == "user":
                counts.append(
                    (
                        len(headers.findall(message.content.text)),
                        len(entries.findall(message.content.text)),
                    )
                )
        return None

    await investigate(pg_url, "s1-a", rules=[check])
    assert counts and all(found == expected for found, expected in counts)
    assert evidence_header(
        EvidenceView("ev_0123456789", "kubernetes", "k8s_get_pod_status", "")
    ) == ("[ev_0123456789] source: kubernetes; tool: k8s_get_pod_status")


async def test_checkpoints_live_in_their_own_schema(migrated: Engine, pg_url: str) -> None:
    result = await investigate(pg_url, "s1-a")

    with migrated.connect() as connection:
        rows = connection.scalar(
            text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :id"),
            {"id": result.investigation.id},
        )
        public = connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'checkpoint%'"
            )
        )
    assert rows and rows > 0
    assert public == 0


def test_command_line_replays_a_case(
    migrated: Engine,
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BIANQUE_DATABASE_URL", pg_url)
    monkeypatch.setenv("BIANQUE_LLM_MODE", "scripted")
    monkeypatch.setenv("BIANQUE_TOOL_BACKEND", "pack")
    monkeypatch.setenv("BIANQUE_CASES_DIR", str(CASES))

    assert main(["investigate", "--case", "s1-a"]) == 0

    out = capsys.readouterr().out
    assert re.search(r"^investigation inv_\w+: reported$", out, re.M)
    assert "plans: v1 (model), v2 (model)" in out
    assert "root cause: identified config_error Deployment/s1-app" in out


def test_command_line_refuses_a_case_on_the_real_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANQUE_TOOL_BACKEND", "live")
    with pytest.raises(SystemExit, match="BIANQUE_TOOL_BACKEND=pack"):
        main(["investigate", "--case", "s1-a"])
