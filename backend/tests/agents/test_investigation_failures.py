"""Failure and fallback paths of the investigation graph, each forced by an injected rule."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from investigation_harness import investigate
from sqlalchemy import Engine, text

from bianque.agents.nodes import BUDGET_EXHAUSTED, RULE_REASON
from bianque.llm import ScriptedReply
from bianque.llm.errors import RetryableProviderError
from bianque.llm.request import ModelRequest

#: A text no role rule writes; it marks model output that must not be stored.
SENTINEL = "SENTINEL-c0ffee"

#: A write command the gate's forbidden action check rejects outside a change suggestion.
WRITE = "kubectl delete pod s1-app"


def _calls_so_far(request: ModelRequest) -> list[str]:
    return [call.name for m in request.prompt.messages for call in m.tool_calls]


def when(
    predicate: Callable[[ModelRequest], bool], reply: Callable[[ModelRequest], ScriptedReply]
) -> Callable[[ModelRequest], ScriptedReply | None]:
    """Return a rule that answers with ``reply`` the requests ``predicate`` selects."""

    def rule(request: ModelRequest) -> ScriptedReply | None:
        return reply(request) if predicate(request) else None

    return rule


def first_turn_of(agent: str) -> Callable[[ModelRequest], bool]:
    def predicate(request: ModelRequest) -> bool:
        return (
            request.agent == agent and request.tool_choice == "auto" and not _calls_so_far(request)
        )

    return predicate


def call(name: str, **arguments: Any) -> Callable[[ModelRequest], ScriptedReply]:
    return lambda request: ScriptedReply(tool_calls=((name, arguments),))


def _table_hits(migrated: Engine, needle: str) -> dict[str, int]:
    """Count rows that contain ``needle`` per table, model call records excluded."""
    hits = {}
    with migrated.connect() as connection:
        tables = connection.scalars(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).all()
        for table in tables:
            where = f"t::text LIKE '%{needle}%'"
            if table == "call_records":
                where += " AND t.kind <> 'model'"
            count = connection.scalar(text(f'SELECT count(*) FROM "{table}" t WHERE {where}'))
            if count:
                hits[table] = int(count)
    return hits


async def test_a_tool_outside_the_registry_is_rejected_and_not_run(
    migrated: Engine, pg_url: str
) -> None:
    """A tool outside the registry is rejected, recorded and never run."""
    rule = when(
        first_turn_of("kubernetes"),
        call("kubectl_delete_pod", namespace="demo-s1", pod="s1-app"),
    )
    result = await investigate(pg_url, "s1-a", rules=[rule])

    rejected = [c for c in result.calls if c.name == "kubectl_delete_pod"]
    assert rejected and all(c.status == "rejected" and c.agent == "kubernetes" for c in rejected)
    assert all((c.error or "").startswith("unknown_tool") for c in rejected)
    assert not [e for e in result.evidence if e.tool == "kubectl_delete_pod"]
    assert result.investigation.status == "reported"


async def test_the_logs_agent_cannot_call_a_change_tool(migrated: Engine, pg_url: str) -> None:
    """The logs sub-agent is refused a change tool and the refusal is recorded."""
    rule = when(first_turn_of("logs"), call("change_list_commits"))
    result = await investigate(pg_url, "s1-a", rules=[rule])

    refused = [c for c in result.calls if c.agent == "logs" and c.name == "change_list_commits"]
    assert refused and all(c.status == "rejected" for c in refused)
    assert all("not a tool of the logs domain" in (c.error or "") for c in refused)
    first_round = {s.id for s in result.steps if s.plan_id == result.plans[0].id}
    assert not [e for e in result.evidence if e.step_id in first_round and e.source == "git"]


async def test_one_model_turn_per_step_still_ends(migrated: Engine, pg_url: str) -> None:
    """With a step limit of one, the investigation still ends."""
    result = await investigate(pg_url, "s1-a", subagent_max_steps=1)

    assert result.investigation.status in ("reported", "degraded")
    assert result.investigation.finished_at is not None
    assert all(s.status in ("done", "failed") for s in result.steps)


async def test_no_replan_round_reports_after_the_first_round(migrated: Engine, pg_url: str) -> None:
    """With no supplementary round allowed, the first assessment leads to the report."""
    result = await investigate(pg_url, "s1-a", max_replan_rounds=0)

    assert [p.version for p in result.plans] == [1]
    assert len(result.assessments) == 1 and not result.assessments[0].sufficient
    assert result.investigation.status in ("reported", "degraded")


async def test_a_failing_sub_agent_does_not_stop_the_others(migrated: Engine, pg_url: str) -> None:
    """The kubernetes sub-agent raises; the logs sub-agent finishes its steps."""

    def explode(request: ModelRequest) -> ScriptedReply | None:
        if request.agent == "kubernetes":
            raise RuntimeError("the kubernetes sub-agent broke")
        return None

    result = await investigate(pg_url, "s1-a", rules=[explode])

    first = result.plans[0].id
    statuses = {(s.domain, s.status) for s in result.steps if s.plan_id == first}
    assert statuses == {("kubernetes", "failed"), ("logs", "done")}
    assert result.investigation.status == "reported"
    assert result.final is not None
    missing = " ".join(m["what"] for m in result.final.body["missing_evidence"])
    assert "kubernetes evidence agent" in missing


async def test_reports_rejected_until_retries_run_out_degrade(
    migrated: Engine, pg_url: str
) -> None:
    """Three rejected reports, then a degraded report and no published one."""

    def bad_report(request: ModelRequest) -> ScriptedReply:
        body = {
            "schema_version": "1",
            "summary": "The pod restarts.",
            "impact": "One pod is not ready.",
            "timeline": [],
            "root_cause": {
                "status": "unknown",
                "statement": "Unknown.",
                "citations": [],
                "confidence": "low",
            },
            "claims": [{"statement": "Something failed.", "citations": ["ev_ffffffffff"]}],
            "missing_evidence": [],
            "suggestions": [],
            "references": [],
            "followup_questions": [],
        }
        return ScriptedReply(tool_calls=(("submit_report", body),))

    rule = when(lambda request: request.operation == "report", bad_report)
    result = await investigate(pg_url, "s1-a", rules=[rule])

    assert [r.status for r in result.reports] == ["rejected"] * 3 + ["degraded"]
    assert result.investigation.status == "degraded"
    rejected = [e for e in result.events if e.type == "report_rejected"]
    assert [e.payload for e in rejected] == [
        {"attempt": n, "violations": ["citation_integrity"]} for n in (1, 2, 3)
    ]
    assert [e.type for e in result.events][-2:] == ["report_degraded", "investigation_finished"]
    assert result.final is not None
    assert result.final.body["root_cause"]["status"] == "unknown"


async def test_a_rejected_plan_falls_back_to_the_default_plan(
    migrated: Engine, pg_url: str
) -> None:
    """The plan carries a write command twice; the default plan replaces it."""

    def bad_plan(request: ModelRequest) -> ScriptedReply:
        plan = {
            "rationale": f"{WRITE} first. {SENTINEL}",
            "steps": [
                {
                    "goal": "Read the pod.",
                    "domain": "kubernetes",
                    "suggested_tools": ["k8s_get_pod_status"],
                    "expected_evidence": "Pod status.",
                }
            ],
            "knowledge_refs": [],
        }
        return ScriptedReply(tool_calls=(("submit_plan", plan),))

    rule = when(lambda request: request.operation == "plan", bad_plan)
    result = await investigate(pg_url, "s1-a", rules=[rule])

    assert result.plans[0].source == "default"
    plan_calls = [c for c in result.calls if c.name == "plan"]
    assert len(plan_calls) == 2
    assert all(c.gate_result and not c.gate_result["passed"] for c in plan_calls)
    created = next(e for e in result.events if e.type == "plan_created")
    assert created.payload["source"] == "default"
    assert _table_hits(migrated, SENTINEL) == {}


async def test_a_rejected_assessment_falls_back_to_the_rule(migrated: Engine, pg_url: str) -> None:
    """The assessment carries a write command twice; the rule decides."""

    def bad_assessment(request: ModelRequest) -> ScriptedReply:
        judged = {
            "sufficient": True,
            "reason": f"{WRITE} would fix it. {SENTINEL}",
            "hypothesis_category": "config_error",
            "missing": [],
        }
        return ScriptedReply(tool_calls=(("submit_assessment", judged),))

    rule = when(lambda request: request.operation == "assess", bad_assessment)
    result = await investigate(pg_url, "s1-a", rules=[rule], max_replan_rounds=0)

    assessment = result.assessments[0]
    assert assessment.rule_override and assessment.reason == RULE_REASON
    assert assessment.hypothesis_category is None
    assert _table_hits(migrated, SENTINEL) == {}


async def test_rejected_findings_are_dropped_and_the_evidence_kept(
    migrated: Engine, pg_url: str
) -> None:
    """Findings cite unknown evidence and carry a write command; they are dropped."""

    def has_results(request: ModelRequest) -> bool:
        return request.operation == "evidence_agent" and (
            request.tool_choice == "submit_findings"
            or any(m.role == "tool" for m in request.prompt.messages)
        )

    findings = {"findings": [{"statement": f"{WRITE} {SENTINEL}", "citations": ["ev_0000000000"]}]}
    rule = when(
        has_results, lambda request: ScriptedReply(tool_calls=(("submit_findings", findings),))
    )
    result = await investigate(pg_url, "s1-a", rules=[rule], max_replan_rounds=0)

    assert result.steps and all(s.findings == [] for s in result.steps)
    assert {e.tool for e in result.evidence if e.tool} >= {"k8s_get_pod_status"}
    assert _table_hits(migrated, SENTINEL) == {}


async def test_an_exhausted_budget_stops_after_the_first_round(
    migrated: Engine, pg_url: str
) -> None:
    """A budget below the first round ends the investigation after that round."""
    result = await investigate(pg_url, "s1-a", investigation_token_budget=1)

    assert [p.version for p in result.plans] == [1]
    assert len(result.assessments) == 1
    assert result.assessments[0].reason.endswith(BUDGET_EXHAUSTED)
    assert result.investigation.status in ("reported", "degraded")


async def test_a_missing_recording_fails_the_investigation(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    """A recorded replay that misses a recording must fail, not fall back."""
    result = await investigate(pg_url, "s1-a", llm_mode="recorded", llm_recordings_dir=tmp_path)

    assert result.investigation.status == "failed"
    assert result.investigation.error == "RecordingNotFoundError"
    assert result.plans == [] and result.final is None
    assert [e.type for e in result.events][-1] == "investigation_finished"


async def test_an_unavailable_model_falls_back_to_the_default_plan(
    migrated: Engine, pg_url: str
) -> None:
    """Retries used up on the plan call: the default plan replaces the model's plan."""

    def unavailable(request: ModelRequest) -> ScriptedReply | None:
        if request.operation == "plan":
            raise RetryableProviderError("timeout", "the model did not answer")
        return None

    result = await investigate(pg_url, "s1-a", rules=[unavailable])

    assert result.plans[0].source == "default"
    assert result.investigation.status == "reported"


async def test_a_tool_failure_survives_a_later_sub_agent_failure(
    migrated: Engine, pg_url: str
) -> None:
    """The logs tool times out, then the logs sub-agent breaks; both count."""

    def break_after_results(request: ModelRequest) -> ScriptedReply | None:
        if request.agent == "logs" and any(m.role == "tool" for m in request.prompt.messages):
            raise RuntimeError("the logs sub-agent broke")
        return None

    result = await investigate(pg_url, "s1-d", rules=[break_after_results])

    assert result.final is not None
    missing = " ".join(m["what"] for m in result.final.body["missing_evidence"])
    assert "logs_get_container_logs" in missing
    assert "logs evidence agent" in missing
    assert result.final.body["root_cause"]["confidence"] != "high"
