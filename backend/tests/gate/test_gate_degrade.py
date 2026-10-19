"""The degraded report: unknown root cause, evidence summary and rule hints, and it passes.

The evidence comes from the S1 evidence packs through the executor, the same path an
investigation takes, so the hint rules read the real tool output.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bianque.agents.domains import domain
from bianque.gate import (
    EvidenceItem,
    FailedCall,
    GateContext,
    OutputType,
    Report,
    build_degraded_report,
    validate,
)
from bianque.redaction import build_engine
from bianque.tools import CallerScope, InvestigationContext, ToolExecutor, registry
from bianque.tools.backends.pack import open_pack
from bianque.tools.store import EvidenceRecord, ToolCallRecord

CASES = Path(__file__).resolve().parents[3] / "eval" / "cases"
NAMESPACE = "demo-s1"
DEPLOYMENT = "s1-app"


class _Evidence:
    def __init__(self) -> None:
        self.records: dict[str, EvidenceRecord] = {}

    async def save(self, call: ToolCallRecord, evidence: EvidenceRecord | None) -> None:
        if evidence is not None:
            self.records[evidence.evidence_id] = evidence


async def _collect(case: str) -> tuple[list[EvidenceRecord], list[FailedCall]]:
    """Run the default plan's tools on a pack, as the default plan would have them run."""
    pack = CASES / case / "pack"
    alert = json.loads((pack / "alert.json").read_text())["alerts"][0]["labels"]
    pod, container = alert["pod"], alert["container"]
    calls: list[tuple[str, dict[str, Any]]] = [
        ("k8s_list_pods", {"namespace": NAMESPACE}),
        ("k8s_get_pod_status", {"namespace": NAMESPACE, "pod": pod}),
        ("k8s_list_events", {"namespace": NAMESPACE, "involved_object": pod}),
        ("k8s_get_deployment", {"namespace": NAMESPACE, "name": DEPLOYMENT}),
        (
            "logs_get_container_logs",
            {"namespace": NAMESPACE, "pod": pod, "container": container, "previous": True},
        ),
        ("change_get_rollout_history", {"namespace": NAMESPACE, "deployment": DEPLOYMENT}),
    ]
    sink = _Evidence()
    failed = []
    async with open_pack(pack) as backends:
        executor = ToolExecutor(backends, sink, engine=build_engine(), sleep=_no_wait)
        for name, args in calls:
            definition = registry.get(name)
            assert definition is not None and definition.domain is not None
            result = await executor.call(
                name,
                args,
                scope=CallerScope(agent=definition.domain, domain=definition.domain),
                investigation=InvestigationContext("inv_test"),
            )
            if result.status != "ok":
                assert result.error is not None
                failed.append(
                    FailedCall(
                        tool=name,
                        domain=definition.domain,
                        error=result.error.split(":", 1)[0],
                        caps_confidence=domain(definition.domain).failure_caps_confidence,
                    )
                )
    return list(sink.records.values()), failed


async def _no_wait(_: float) -> None:
    return None


def _items(records: Sequence[EvidenceRecord]) -> list[EvidenceItem]:
    return [
        EvidenceItem(r.evidence_id, r.source, r.tool, r.collected_at, r.redacted_content)
        for r in records
    ]


def _context(records: Sequence[EvidenceRecord], failed: Sequence[FailedCall]) -> GateContext:
    """The context an investigation builds: tool names are preserved like identifiers."""
    return GateContext(
        evidence_ids=frozenset(r.evidence_id for r in records),
        preserved=frozenset(i for r in records for i in r.identifiers) | {t.name for t in registry},
        failed_calls=tuple(failed),
    )


def _hints(report: Report) -> set[str]:
    return {
        claim.statement.rsplit(" ", 1)[-1].rstrip(".")
        for claim in report.claims
        if claim.statement.startswith("Rule hint:")
    }


@pytest.mark.parametrize(
    ("case", "hints"),
    [
        ("s1-a", set()),
        ("s1-b", {"resource_limit"}),
        ("s1-c", {"probe_misconfig"}),
        ("s1-d", set()),
        ("s1-e", set()),
        ("s1-f", set()),
    ],
)
async def test_degraded_report_of_each_case_passes_the_gate(case: str, hints: set[str]) -> None:
    records, failed = await _collect(case)
    report = build_degraded_report(_items(records), failed, ["citation_integrity"])

    result = validate(OutputType.REPORT, report, _context(records, failed))

    assert result.violations == ()
    assert report.root_cause.status == "unknown"
    assert report.root_cause.confidence == "low"
    assert report.root_cause.category is None
    assert report.suggestions == []
    assert _hints(report) == hints
    cited = {c for claim in report.claims for c in claim.citations}
    assert cited == {r.evidence_id for r in records}
    assert len(report.missing_evidence) == len({(f.tool, f.error) for f in failed})


async def test_failed_calls_become_missing_evidence() -> None:
    records, failed = await _collect("s1-d")
    assert [(f.tool, f.error, f.caps_confidence) for f in failed] == [
        ("logs_get_container_logs", "timeout", True)
    ]
    report = build_degraded_report(_items(records), failed, [])
    assert [m.what for m in report.missing_evidence] == [
        "Result of logs_get_container_logs (logs domain)."
    ]
    assert "timeout" in report.missing_evidence[0].reason


async def test_case_without_most_evidence_lists_what_is_missing() -> None:
    records, failed = await _collect("s1-f")
    assert {f.tool for f in failed} >= {"k8s_get_deployment", "logs_get_container_logs"}
    report = build_degraded_report(_items(records), failed, [])
    assert len(report.missing_evidence) == len(failed)


async def test_termination_is_on_the_timeline() -> None:
    records, failed = await _collect("s1-b")
    report = build_degraded_report(_items(records), failed, [])
    events = [entry.event for entry in report.timeline]
    assert events and all("OOMKilled" in event for event in events)
    assert [e.time for e in report.timeline] == sorted(e.time for e in report.timeline)


def test_summary_names_the_rejecting_validators_once() -> None:
    report = build_degraded_report([], [], ["schema", "citation_integrity", "schema"])
    assert "(citation_integrity, schema)" in report.summary


def test_output_does_not_depend_on_evidence_order() -> None:
    at = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    pod_status = json.dumps(
        {
            "name": "s1-app-6867fdfbd4-bpv2m",
            "containers": [
                {
                    "name": "app",
                    "state": {"state": "waiting", "reason": "CrashLoopBackOff"},
                    "last_state": {
                        "state": "terminated",
                        "reason": "OOMKilled",
                        "exit_code": 137,
                        "finished_at": "2026-10-19T11:59:00Z",
                    },
                }
            ],
        }
    )
    items = [
        EvidenceItem("ev_00000000b2", "kubernetes", "k8s_get_pod_status", at, pod_status),
        EvidenceItem("ev_00000000a1", "alertmanager", None, at, "{}"),
    ]
    forward = build_degraded_report(items, [], ["schema"])
    backward = build_degraded_report(list(reversed(items)), [], ["schema"])
    assert forward == backward
    assert _hints(forward) == {"resource_limit"}


def test_unparseable_evidence_is_only_summarized() -> None:
    at = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    item = EvidenceItem("ev_00000000c3", "kubernetes", "k8s_get_pod_status", at, "not json")
    report = build_degraded_report([item], [], [])
    assert [c.citations for c in report.claims] == [["ev_00000000c3"]]
    assert report.timeline == []
