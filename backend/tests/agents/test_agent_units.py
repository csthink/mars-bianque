"""Units of the investigation agent: range check, prompt sections, alert mapping, the rule."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from investigation_harness import investigate
from sqlalchemy import Engine

from bianque.agents.default_plan import default_plan
from bianque.agents.nodes import check_range
from bianque.agents.prompting import (
    EvidenceView,
    FailureView,
    FindingsView,
    render_evidence,
    render_failures,
    render_findings,
    render_violations,
)
from bianque.api.alertmanager import AlertFormatError, parse_webhook
from bianque.gate import Plan, PlanStep, Violation
from bianque.llm import ScriptedReply
from bianque.llm.request import ModelRequest


def _step(domain: str) -> PlanStep:
    return PlanStep(
        goal="Read something.", domain=domain, suggested_tools=[], expected_evidence="Something."
    )


def test_first_round_drops_later_and_unknown_domains() -> None:
    plan = Plan(
        rationale="Look.",
        steps=[_step("kubernetes"), _step("change"), _step("logs"), _step("network")],
        knowledge_refs=[],
    )
    kept, dropped = check_range(plan, first_round=True)
    assert [s.domain for s in kept] == ["kubernetes", "logs"]
    assert dropped == [{"index": 1, "domain": "change"}, {"index": 3, "domain": "network"}]

    kept, dropped = check_range(plan, first_round=False)
    assert [s.domain for s in kept] == ["kubernetes", "change", "logs"]
    assert dropped == [{"index": 3, "domain": "network"}]


def test_default_plans_pass_their_own_range_check() -> None:
    for first_round in (True, False):
        plan = default_plan(first_round=first_round)
        assert check_range(plan, first_round=first_round)[1] == []


def test_sections_are_sorted_whatever_the_input_order() -> None:
    evidence = [
        EvidenceView("ev_bbbbbbbbbb", "git", "change_list_commits", "b"),
        EvidenceView("ev_aaaaaaaaaa", "kubernetes", "k8s_list_pods", "a"),
    ]
    assert render_evidence(evidence) == render_evidence(reversed(evidence))
    assert render_evidence(evidence).startswith("[ev_aaaaaaaaaa]")

    failures = [
        FailureView("logs", "logs_get_container_logs", "timeout"),
        FailureView("change", None, "failed: RuntimeError"),
        FailureView("logs", "logs_get_container_logs", "timeout"),
    ]
    assert render_failures(failures).splitlines() == [
        "- the change evidence agent (change domain): failed: RuntimeError",
        "- logs_get_container_logs (logs domain): timeout",
    ]

    findings = [
        FindingsView(2, 1, "change", "Second.", ({"statement": "B.", "citations": ["ev_b"]},)),
        FindingsView(1, 2, "logs", "First.", ()),
    ]
    assert render_findings(findings).splitlines()[0] == "Plan 1 step 2 (logs): First."
    assert "- no findings" in render_findings(findings)

    violations = [Violation("schema", "b", "x"), Violation("citation_integrity", "a", "y")]
    assert render_violations(violations).splitlines()[0].startswith("- citation_integrity")


def test_alert_mapping_keeps_the_names_of_alerted_objects() -> None:
    body = {
        "alerts": [
            {
                "fingerprint": "f1",
                "status": "firing",
                "labels": {
                    "alertname": "BianquePodRestarting",
                    "namespace": "demo-s1",
                    "pod": "s1-app-5468546b65-dvgq5",
                    "severity": "warning",
                },
                "annotations": {"summary": "restarting"},
                "startsAt": "2026-10-19T22:33:27.664Z",
            }
        ]
    }
    alert = parse_webhook(body)[0]
    assert alert.identifiers == ("BianquePodRestarting", "demo-s1", "s1-app-5468546b65-dvgq5")
    assert alert.starts_at == datetime(2026, 10, 19, 22, 33, 27, 664000, tzinfo=UTC)
    assert alert.text.startswith('{\n  "annotations"')

    with pytest.raises(AlertFormatError):
        parse_webhook({"alerts": []})
    with pytest.raises(AlertFormatError):
        parse_webhook({"alerts": [{"status": "firing"}]})


async def test_the_rule_overrides_a_premature_sufficient(migrated: Engine, pg_url: str) -> None:
    """A config hypothesis without change evidence is never sufficient."""

    def eager(request: ModelRequest) -> ScriptedReply | None:
        if request.operation != "assess":
            return None
        judged = {
            "sufficient": True,
            "reason": "The log names the invalid value.",
            "hypothesis_category": "config_error",
            "missing": [],
        }
        return ScriptedReply(tool_calls=(("submit_assessment", judged),))

    result = await investigate(pg_url, "s1-a", rules=[eager])

    first = result.assessments[0]
    assert not first.sufficient and first.rule_override
    assert "Evidence of the change domain." in first.missing
    assert [p.version for p in result.plans] == [1, 2]
    assert result.assessments[1].sufficient and not result.assessments[1].rule_override
