"""The auxiliary tools that read investigations: scoped by namespace."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from tool_fakes import MemorySink, backends, no_sleep

from bianque.redaction import build_engine
from bianque.tools import CallerScope, InvestigationContext, ToolExecutor, registry
from bianque.tools.backends.base import InvestigationReport, InvestigationRow

INVESTIGATION = "inv_01M5CMN7F732AM2B6JP2PFKGNP"
POD = "s1-app-5468546b65-dvgq5"
STARTED = datetime(2026, 10, 20, 12, 30, 24, tzinfo=UTC)

ROW = InvestigationRow(
    id=INVESTIGATION,
    status="reported",
    case_id="s1-a",
    alert_name="BianquePodRestarting",
    namespace="demo-s1",
    pod=POD,
    started_at=STARTED,
    finished_at=None,
    report_status="published",
)
BODY = {"summary": f"Pod {POD} keeps restarting since commit 9b9a6ea8a72b4e1c."}

MCP = CallerScope(
    agent="mcp",
    tools=frozenset({"investigation_list", "investigation_get_report"}),
    namespaces=frozenset({"demo-s1", "other"}),
    caller="claude-code",
)


class _Investigations:
    def __init__(self, report: dict[str, str] | None = BODY) -> None:
        self.report = report
        self.listed: list[tuple[str, int]] = []
        self.read: list[str] = []

    async def list_investigations(self, namespace: str, limit: int) -> list[InvestigationRow]:
        self.listed.append((namespace, limit))
        return [ROW] if namespace == "demo-s1" else []

    async def get_report(self, investigation_id: str) -> InvestigationReport | None:
        self.read.append(investigation_id)
        if investigation_id != INVESTIGATION:
            return None
        return InvestigationReport(ROW, self.report, (POD, "9b9a6ea8a72b4e1c"))


def _executor(source: _Investigations | None, sink: MemorySink) -> ToolExecutor:
    return ToolExecutor(
        replace(backends(), investigations=source),
        sink,
        tools=registry,
        engine=build_engine(),
        sleep=no_sleep,
    )


async def test_the_list_returns_the_namespace_s_investigations() -> None:
    source, sink = _Investigations(), MemorySink()
    result = await _executor(source, sink).call(
        "investigation_list", {"namespace": "demo-s1", "limit": 3}, scope=MCP
    )
    assert result.status == "ok" and result.evidence_ids == ()
    assert source.listed == [("demo-s1", 3)]
    assert json.loads(result.model_view.text) == {
        "namespace": "demo-s1",
        "investigations": [
            {
                "investigation_id": INVESTIGATION,
                "status": "reported",
                "case": "s1-a",
                "alert": "BianquePodRestarting",
                "namespace": "demo-s1",
                "pod": POD,
                "started_at": "2026-10-20T12:30:24Z",
                "finished_at": None,
                "report_status": "published",
            }
        ],
    }


async def test_the_report_keeps_the_names_of_its_evidence_and_stores_no_evidence() -> None:
    sink = MemorySink()
    result = await _executor(_Investigations(), sink).call(
        "investigation_get_report",
        {"namespace": "demo-s1", "investigation_id": INVESTIGATION},
        scope=MCP,
        investigation=InvestigationContext("inv_01TEST"),
    )
    assert result.status == "ok" and result.evidence_ids == ()
    data = json.loads(result.model_view.text)
    assert data["report"] == BODY  # the Pod name and commit hash survive redaction
    assert data["report_status"] == "published"
    assert sink.evidence == []


async def test_a_report_outside_the_namespace_or_missing_is_not_found() -> None:
    sink = MemorySink()
    executor = _executor(_Investigations(report=None), sink)
    elsewhere = await executor.call(
        "investigation_get_report",
        {"namespace": "other", "investigation_id": INVESTIGATION},
        scope=MCP,
    )
    unknown = await executor.call(
        "investigation_get_report",
        {"namespace": "demo-s1", "investigation_id": "inv_01M5CMN7F732AM2B6JP2PFKGNQ"},
        scope=MCP,
    )
    unreported = await executor.call(
        "investigation_get_report",
        {"namespace": "demo-s1", "investigation_id": INVESTIGATION},
        scope=MCP,
    )
    assert elsewhere.error == f"not_found: investigation {INVESTIGATION} in other"
    assert unknown.status == "error" and unknown.error is not None
    assert unknown.error.startswith("not_found: investigation")
    assert unreported.error == f"not_found: report of investigation {INVESTIGATION}"


async def test_an_investigation_id_is_validated_before_any_lookup() -> None:
    source, sink = _Investigations(), MemorySink()
    result = await _executor(source, sink).call(
        "investigation_get_report",
        {"namespace": "demo-s1", "investigation_id": "'; drop table reports; --"},
        scope=MCP,
    )
    assert result.status == "rejected"
    assert result.error is not None and result.error.startswith("invalid_arguments")
    assert source.read == []


async def test_without_a_source_the_tools_fail() -> None:
    result = await _executor(None, MemorySink()).call(
        "investigation_list", {"namespace": "demo-s1"}, scope=MCP
    )
    assert result.status == "error"
    assert result.error == "failed: investigations"
