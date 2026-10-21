"""Evaluation metrics on hand-made runs: the definitions, one by one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bianque.evals.cases import Case, EvidenceSpec, Question
from bianque.evals.metrics import (
    Answer,
    CallRow,
    CaseRun,
    EvidenceRow,
    ReportRow,
    case_result,
    citation_completeness,
    gate_stats,
    question_result,
    required_evidence,
    root_cause,
    summary,
)
from bianque.evals.report import (
    EvalReport,
    LLMContext,
    compare_reports,
    load_report,
    render_comparison,
    render_markdown,
)
from bianque.evals.run import hard_gates

EVIDENCE = (
    EvidenceRow("ev_alert", "alertmanager", None, {}),
    EvidenceRow(
        "ev_pod", "kubernetes", "k8s_get_pod_status", {"namespace": "demo-s1", "pod": "p-1"}
    ),
    EvidenceRow("ev_diff", "git", "change_show_commit", {"commit": "9b9a6ea8a72b"}),
    EvidenceRow("ev_old", "git", "change_show_commit", {"commit": "339332f21347"}),
)


def _case(**expected: Any) -> Case:
    base: dict[str, Any] = {
        "root_cause": {
            "status": "identified",
            "category": "config_error",
            "object": {"kind": "Deployment", "namespace": "demo-s1", "name": "s1-app"},
        },
        "required_evidence": [
            {"tool": "k8s_get_pod_status", "args": {"pod": "p-*"}},
            {"tool": "change_show_commit", "args": {"commit": "9b9a6ea*"}},
            {"tool": "k8s_get_deployment"},
        ],
        "root_cause_evidence": [{"tool": "change_show_commit", "args": {"commit": "9b9a6ea*"}}],
    }
    return Case.model_validate(
        {"schema_version": 1, "case": "s1-x", "title": "x", "expected": {**base, **expected}}
    )


def _body(cause: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "timeline": [{"citations": ["ev_pod"]}, {"citations": []}],
        "claims": [{"citations": ["ev_pod", "ev_nope"]}],
        "suggestions": [{"citations": ["ev_diff"]}],
        "root_cause": cause
        or {
            "status": "identified",
            "category": "config_error",
            "object": {"kind": "Deployment", "namespace": "demo-s1", "name": "s1-app"},
            "citations": ["ev_diff"],
            "confidence": "high",
        },
        "missing_evidence": [],
    }
    return {**body, **fields}


def _run(case: Case | None = None, reports: tuple[ReportRow, ...] = (), **fields: Any) -> CaseRun:
    values: dict[str, Any] = {
        "case": case or _case(),
        "investigation_id": "inv_1",
        "status": "reported",
        "error": None,
        "duration_s": 1.0,
        "tokens": 10,
        "token_budget": 100,
        "plan_sources": ["v1 (model)"],
        "rule_overrides": 0,
        "evidence": EVIDENCE,
        "suggestion_ids": frozenset({"sg_1"}),
        "calls": (),
        "followup_calls": (),
        "reports": reports or (ReportRow(1, "published", _body()),),
    }
    return CaseRun(**{**values, **fields})


def test_citation_completeness_counts_the_first_attempt() -> None:
    """Valid: non-empty and every ID is evidence; the first attempt, before the gate."""
    run = _run(
        reports=(
            ReportRow(1, "rejected", _body()),
            ReportRow(2, "published", _body(timeline=[], claims=[])),
        )
    )
    ratio = citation_completeness(run)
    # timeline: valid, empty; claim: unknown ID; suggestion: valid; root cause: valid.
    assert (ratio.count, ratio.total) == (3, 5)


def test_an_unknown_root_cause_is_not_a_conclusion() -> None:
    cause = {"status": "unknown", "statement": "?", "citations": [], "confidence": "low"}
    run = _run(reports=(ReportRow(1, "published", _body(cause, timeline=[], claims=[])),))
    assert (citation_completeness(run).count, citation_completeness(run).total) == (1, 1)


def test_root_cause_is_judged_by_category_and_object() -> None:
    assert root_cause(_run()).correct
    assert root_cause(_run()).cites_expected_evidence
    wrong = dict(_body()["root_cause"], category="resource_limit")
    assert not root_cause(_run(reports=(ReportRow(1, "published", _body(wrong)),))).correct
    other = dict(
        _body()["root_cause"], object={"kind": "Deployment", "namespace": "x", "name": "s1-app"}
    )
    assert not root_cause(_run(reports=(ReportRow(1, "published", _body(other)),))).correct
    old = dict(_body()["root_cause"], citations=["ev_old"])
    assert not root_cause(
        _run(reports=(ReportRow(1, "published", _body(old)),))
    ).cites_expected_evidence


def test_an_unknown_case_needs_an_unknown_root_cause() -> None:
    case = _case(root_cause={"status": "unknown"}, required_evidence=[], root_cause_evidence=[])
    assert not root_cause(_run(case)).correct
    cause = {"status": "unknown", "statement": "?", "citations": [], "confidence": "low"}
    assert root_cause(_run(case, reports=(ReportRow(1, "degraded", _body(cause)),))).correct


def test_confidence_and_missing_evidence_checks_of_s1d() -> None:
    case = _case(confidence_not=["high"], missing_evidence_mentions=["log", "日志"])
    result = root_cause(_run(case))
    assert result.confidence_ok is False and result.missing_evidence_ok is False
    medium = dict(_body()["root_cause"], confidence="medium")
    missing = [{"what": "容器日志", "reason": "工具超时"}]
    result = root_cause(
        _run(case, reports=(ReportRow(1, "published", _body(medium, missing_evidence=missing)),))
    )
    assert result.confidence_ok is True and result.missing_evidence_ok is True


def test_required_evidence_matches_cited_evidence_only() -> None:
    ratio, matches = required_evidence(_run())
    assert (ratio.count, ratio.total) == (2, 3)
    assert [m.matched for m in matches] == [["ev_pod"], ["ev_diff"], []]


def _gated(call_id: int, output_type: str, passed: bool, agent: str = "investigation") -> CallRow:
    violations = [] if passed else [{"validator": "citation_integrity", "path": "x", "code": "c"}]
    result = {"output_type": output_type, "passed": passed, "violations": violations}
    return CallRow(call_id, agent, "model", output_type, "ok", None, result, 1)


def test_gate_generations_first_pass_retries_and_fallbacks() -> None:
    calls = [
        _gated(1, "plan", False),
        _gated(2, "plan", True),  # retried, passed
        _gated(3, "findings", True, agent="kubernetes"),
        _gated(4, "findings", False, agent="logs"),
        _gated(5, "findings", True, agent="kubernetes"),
        _gated(6, "findings", False, agent="logs"),  # two rejections: fallback
        _gated(7, "report", False),
        _gated(8, "report", False),
        _gated(9, "report", False),  # three report attempts: degraded
        CallRow(10, "investigation", "knowledge", "retrieve", "ok", None, None, 0),
    ]
    stats = gate_stats(calls, report_attempts=3)
    plan, findings, report = stats["plan"], stats["findings"], stats["report"]
    assert (plan.generations, plan.first_pass.count, plan.attempts, plan.fallbacks) == (1, 0, 2, 0)
    assert plan.mean_retries == 1.0
    assert (findings.generations, findings.first_pass.count, findings.fallbacks) == (3, 2, 1)
    assert (report.generations, report.attempts, report.fallbacks) == (1, 3, 1)
    assert report.rejected_by == {"citation_integrity": 3}
    assert stats["assessment"].generations == 0 and stats["assessment"].mean_retries is None


def test_a_question_result_against_its_annotation() -> None:
    question = Question(
        id="q1",
        question="?",
        expected_evidence=(EvidenceSpec(tool="change_show_commit", args={"commit": "9b9a6ea*"}),),
    )
    segments: list[dict[str, Any]] = [
        {"text": "a", "citations": ["ev_diff"]},
        {"text": "b", "citations": ["ev_nope"]},
        {"text": "c", "citations": [], "insufficient_evidence": True},
    ]
    result = question_result(
        Answer(question, "answered", segments, unknown=True), EVIDENCE, frozenset({"sg_1"})
    )
    # The insufficient segment needs no citation and is not counted.
    assert (result.cited_segments.count, result.cited_segments.total) == (1, 2)
    assert result.expected_evidence_hit and result.unknown
    assert result.suggestion_cited is None

    asks_for_suggestion = Question(id="q2", question="?", expected_suggestions=True)
    cited = [{"text": "a", "citations": ["sg_1"]}]
    result = question_result(
        Answer(asks_for_suggestion, "answered", cited), EVIDENCE, frozenset({"sg_1"})
    )
    assert result.suggestion_cited and result.cited_segments.value == 1.0
    assert result.expected_evidence_hit is None


def test_summary_sums_counts_before_dividing() -> None:
    first = case_result(_run(), report_attempts=3)
    case = _case(root_cause={"status": "unknown"}, required_evidence=[], root_cause_evidence=[])
    second = case_result(_run(case), report_attempts=3)
    total = summary([first, second])
    assert (total.root_cause_correct.count, total.root_cause_correct.total) == (1, 2)
    assert (total.citation_completeness.count, total.citation_completeness.total) == (6, 10)
    assert (total.required_evidence.count, total.required_evidence.total) == (2, 3)


def _report(mode: str, *results: Any) -> EvalReport:
    return EvalReport(
        schema_version=2,
        generated_at="2026-10-20T00:00:00Z",  # type: ignore[arg-type]
        passed=True,
        llm=LLMContext(mode=mode, model="m", extra_body={}, recorded_at=None, note=None),  # type: ignore[arg-type]
        embedding={"mode": "fixture"},
        prompts={},
        tool_schemas_sha256="0",
        network_isolated=None,
        hard_gates=[],
        summary=summary(list(results)),
        cases=list(results),
        retrieval={},
    )


def test_compare_finds_what_the_replay_did_not_reproduce() -> None:
    live = case_result(_run(), report_attempts=3)
    same = case_result(_run(investigation_id="inv_2", duration_s=9.0), report_attempts=3)
    assert compare_reports(_report("live", live), _report("recorded", same)) == []
    other = case_result(_run(tokens=11), report_attempts=3)
    assert compare_reports(_report("live", live), _report("recorded", other)) == [
        "s1-x: tokens differs"
    ]
    skilled = case_result(
        _run(calls=(_skill_call(1, "investigate-pod-restarts"),)), report_attempts=3
    )
    assert compare_reports(_report("live", live), _report("recorded", skilled)) == [
        "s1-x: skills differs"
    ]
    assert "not recorded" in compare_reports(_report("live", live), _report("scripted", same))[0]
    assert "# Bianque evaluation" in render_markdown(_report("live", live))


@pytest.mark.parametrize(
    ("change", "gate"),
    [
        ({"status": "failed", "error": "RecordingNotFoundError"}, "investigations_report"),
        (
            {"evidence": (*EVIDENCE, EvidenceRow("ev_x", "investigation", None, {}))},
            "no_agent_evidence",
        ),
        (
            {"reports": (ReportRow(1, "published", _body(claims=[{"citations": ["kn_1"]}])),)},
            "knowledge_not_cited",
        ),
    ],
)
def test_each_hard_gate_fails_on_its_violation(change: dict[str, Any], gate: str) -> None:
    gates = {g.name: g for g in hard_gates([_run(**change)], [], {"kn_1"})}
    assert [name for name, g in gates.items() if not g.passed] == [gate]
    assert gates[gate].details[0].startswith("s1-x: ")


def test_a_failed_revalidation_fails_its_gate() -> None:
    gates = {g.name: g for g in hard_gates([_run()], ["s1-x: final report rejected"], set())}
    assert not gates["outputs_pass_the_gate"].passed


def _skill_call(call_id: int, name: str) -> CallRow:
    return CallRow(call_id, "investigation", "skill", name, "ok", None, None, 0)


def test_a_case_counts_the_activations_of_each_skill() -> None:
    calls = (
        _skill_call(1, "investigate-pod-restarts"),
        _gated(2, "plan", True),
        _skill_call(3, "investigate-pod-restarts"),
        _skill_call(4, "diagnose-config-error"),
    )
    result = case_result(_run(calls=calls), report_attempts=3)
    assert result.skills == {"diagnose-config-error": 1, "investigate-pod-restarts": 2}
    # Activations are not model calls.
    assert result.model_calls == 1
    assert summary([result]).skill_activations == 3
    assert "investigate-pod-restarts ×2" in render_markdown(_report("live", result))


def test_a_report_written_before_skills_reads_without_them(tmp_path: Path) -> None:
    result = case_result(_run(), report_attempts=3)
    document = _report("live", result).model_dump(mode="json")
    document["schema_version"] = 1
    del document["cases"][0]["skills"]
    del document["summary"]["skill_activations"]
    path = tmp_path / "report.json"
    path.write_text(json.dumps(document))
    old = load_report(path)
    assert old.schema_version == 1
    assert old.cases[0].skills is None and old.summary.skill_activations is None


def test_the_comparison_lists_each_case_of_both_reports() -> None:
    base = case_result(_run(tokens=100), report_attempts=3)
    base = base.model_copy(update={"skills": None})
    calls = (_skill_call(1, "investigate-pod-restarts"), _gated(2, "plan", True))
    new = case_result(_run(tokens=150, calls=calls), report_attempts=3)
    text = render_comparison(_report("live", base), _report("live", new))
    assert "| s1-x | config_error, correct | config_error, correct |" in text
    assert "| 100 | 150 |" in text
    assert "investigate-pod-restarts" in text
    assert "one live run each" in text
    total = next(line for line in text.splitlines() if line.startswith("| all |"))
    assert "100" in total and "150" in total
