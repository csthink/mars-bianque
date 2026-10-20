"""The read interface against a real database holding the S1-a fixture investigation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from investigation_fixture import FIXTURE, load, read_fixture
from sqlalchemy.ext.asyncio import AsyncSession

from bianque.api.app import create_app
from bianque.config import Settings
from bianque.gate import GateContext, OutputType, validate
from bianque.storage.repositories import (
    CallRecordRepository,
    ConfirmationRepository,
    InvestigationRepository,
)
from bianque.tools import registry

DATA = read_fixture()
INV = DATA["investigation"]["id"]


@pytest.fixture
async def loaded(session: AsyncSession) -> str:
    return await load(session, FIXTURE)


@pytest.fixture
def client(loaded: str, pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=pg_url))) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


async def test_list_newest_first(session: AsyncSession, loaded: str, pg_url: str) -> None:
    later = await InvestigationRepository(session).create(
        trigger="manual", tool_backend="pack", llm_mode="scripted", token_budget=1000
    )
    await session.commit()
    with TestClient(create_app(Settings(database_url=pg_url))) as client:
        rows = client.get("/api/investigations").json()["investigations"]
    assert [row["id"] for row in rows] == [later.id, INV]
    assert rows[1] == {
        "id": INV,
        "status": "reported",
        "trigger": "replay",
        "case_id": "s1-a",
        "alert_name": "BianquePodRestarting",
        "namespace": "demo-s1",
        "pod": DATA["alert"]["labels"]["pod"],
        "started_at": "2026-10-19T22:33:58Z",
        "finished_at": "2026-10-19T22:35:22Z",
        "report_status": "published",
    }
    assert rows[0]["alert_name"] is None and rows[0]["report_status"] is None


def test_detail_assembles_the_investigation(client: TestClient) -> None:
    detail = client.get(f"/api/investigations/{INV}").json()
    assert detail["investigation"]["status"] == "reported"
    assert detail["alert"]["name"] == "BianquePodRestarting"
    alert_evidence = next(e for e in DATA["evidence"] if e["source"] == "alertmanager")
    assert detail["alert"]["evidence_id"] == alert_evidence["id"]
    assert [(p["version"], len(p["steps"])) for p in detail["plans"]] == [(1, 6), (2, 3)]
    assert {s["domain"] for s in detail["plans"][1]["steps"]} == {"change"}
    assert all(s["findings"] for p in detail["plans"] for s in p["steps"])
    assert [(a["round"], a["sufficient"]) for a in detail["assessments"]] == [(1, False), (2, True)]
    assert [e["id"] for e in detail["evidence"]] == sorted(e["id"] for e in DATA["evidence"])
    assert detail["report"]["status"] == "published"
    assert detail["report"]["attempt"] == 2
    assert detail["rejected_attempts"] == [
        {
            "attempt": 1,
            "validators": ["citation_integrity"],
            "created_at": "2026-10-19T22:35:06.200000Z",
        }
    ]
    assert [(s["seq"], s["risk"], s["state"]) for s in detail["suggestions"]] == [
        (1, "change", "pending"),
        (2, "read_only", "pending"),
    ]


def test_evidence_is_returned_redacted_only(client: TestClient) -> None:
    text = client.get(f"/api/investigations/{INV}").text
    pods = next(e for e in DATA["evidence"] if e["tool"] == "k8s_list_pods")
    pod_uid = "9c3517b4-6285-4696-922f-fbb7d024fd80"
    assert pod_uid in pods["raw_content"] and pod_uid not in pods["redacted_content"]
    assert pod_uid not in text
    assert "raw_content" not in text


def test_rejected_output_and_model_summaries_are_not_returned(client: TestClient) -> None:
    """No rejected report body and no model call summary reaches a client."""
    detail = client.get(f"/api/investigations/{INV}").text
    calls = client.get(f"/api/investigations/{INV}/calls").text
    for sentinel in ("XREJECTEDX", "XMODELOUTX", "XMODELINX"):
        assert sentinel in json.dumps(DATA), sentinel
        assert sentinel not in detail, sentinel
        assert sentinel not in calls, sentinel
    assert "claims[1].citations[0]" not in detail
    assert "unknown_id" not in detail


def test_calls_and_totals(client: TestClient) -> None:
    body = client.get(f"/api/investigations/{INV}/calls").json()
    calls, totals = body["calls"], body["totals"]
    model = [c for c in DATA["calls"] if c["kind"] == "model"]
    knowledge = [c for c in DATA["calls"] if c["kind"] == "knowledge"]
    assert len(knowledge) == 1
    assert totals == {
        "calls": len(DATA["calls"]),
        "model_calls": len(model),
        "tool_calls": len(DATA["calls"]) - len(model) - len(knowledge),
        "knowledge_calls": 1,
        "tokens_in": sum(c["tokens_in"] for c in model),
        "tokens_out": sum(c["tokens_out"] for c in model),
        "latency_ms": sum(c["latency_ms"] for c in DATA["calls"]),
        "elapsed_ms": 84000,
    }
    assert totals["tokens_in"] + totals["tokens_out"] == DATA["investigation"]["tokens_used"]
    starts = [datetime.fromisoformat(c["started_at"]) for c in calls]
    assert starts == sorted(starts)
    for call in calls:
        if call["kind"] == "model":
            assert call["input_summary"] is None and call["output_summary"] is None
            assert call["evidence_ids"] == []
        elif call["kind"] == "knowledge":
            # Knowledge is not model output: the query and the chunks found are shown,
            # and it produces no evidence.
            assert call["name"] == "retrieve_knowledge" and call["agent"] == "investigation"
            assert call["input_summary"].startswith("Bianque Pod Restarting")
            assert call["output_summary"].startswith("kn_c73d8439117f ")
            assert call["evidence_ids"] == [] and call["gate"] is None
        else:
            assert call["input_summary"] and call["output_summary"]
            assert len(call["evidence_ids"]) == 1 and call["gate"] is None
    rejected = [c for c in calls if c["gate"] and not c["gate"]["passed"]]
    assert [(c["name"], c["gate"]["validators"]) for c in rejected] == [
        ("report", ["citation_integrity"])
    ]


async def test_decided_suggestion_shows_its_state(
    session: AsyncSession, loaded: str, pg_url: str
) -> None:
    with TestClient(create_app(Settings(database_url=pg_url))) as client:
        first = client.get(f"/api/investigations/{INV}").json()["suggestions"][0]
    await ConfirmationRepository(session).decide(
        INV, first["id"], decision="confirmed", actor="demo"
    )
    await session.commit()
    with TestClient(create_app(Settings(database_url=pg_url))) as client:
        again = client.get(f"/api/investigations/{INV}").json()["suggestions"][0]
    assert again["state"] == "confirmed" and again["decided_by"] == "demo"


def test_unknown_investigation_is_404(client: TestClient) -> None:
    assert client.get("/api/investigations/inv_missing").status_code == 404
    assert client.get("/api/investigations/inv_missing/calls").status_code == 404


async def test_elapsed_runs_to_the_last_call_while_running(
    session: AsyncSession, loaded: str, pg_url: str
) -> None:
    running = await InvestigationRepository(session).create(
        trigger="manual", tool_backend="pack", llm_mode="scripted", token_budget=1000
    )
    start = running.started_at
    await CallRecordRepository(session).add(
        investigation_id=running.id,
        agent="investigation",
        kind="model",
        name="plan",
        input_summary="x",
        status="ok",
        started_at=start + timedelta(seconds=2),
        latency_ms=3500,
        tokens_in=10,
        tokens_out=5,
    )
    await session.commit()
    with TestClient(create_app(Settings(database_url=pg_url))) as client:
        totals = client.get(f"/api/investigations/{running.id}/calls").json()["totals"]
    assert totals["elapsed_ms"] == 5500
    assert start.tzinfo is not None and start < datetime.now(UTC)


def test_fixture_report_passes_the_gate() -> None:
    """The fixture stays close to what a real investigation stores."""
    evidence = DATA["evidence"]
    knowledge = next(c for c in DATA["calls"] if c["kind"] == "knowledge")
    context = GateContext(
        evidence_ids=frozenset(e["id"] for e in evidence),
        knowledge_ids=frozenset(
            line.split(" ", 1)[0] for line in knowledge["output_summary"].splitlines()
        ),
        preserved=frozenset(i for e in evidence for i in e["identifiers"])
        | {t.name for t in registry},
    )
    published = next(r for r in DATA["reports"] if r["status"] == "published")
    assert validate(OutputType.REPORT, published["body"], context).passed
    rejected = next(r for r in DATA["reports"] if r["status"] == "rejected")
    assert validate(OutputType.REPORT, rejected["body"], context).validators() == [
        "citation_integrity"
    ]
