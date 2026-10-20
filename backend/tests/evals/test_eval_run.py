"""The case evaluation end to end, in scripted mode on the test database."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import bianque.evals.run as eval_run
from bianque.agents.service import case_alert
from bianque.config import Settings
from bianque.evals.metrics import ReportRow, final_report
from bianque.evals.report import EvalReport, write_report
from bianque.evals.run import EvaluationError, evaluate, load_cases, revalidate
from bianque.gate import validate
from bianque.storage.db import session_factory

REPO = Path(__file__).resolve().parents[3]
CASES = REPO / "eval" / "cases"
QUERIES = REPO / "eval" / "retrieval" / "queries.yaml"
RUNBOOKS = REPO / "knowledge" / "runbooks"
EMBEDDING_FIXTURE = REPO / "eval" / "embeddings" / "bge-small-en-v1.5.json"


def _settings(pg_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": pg_url,
        "llm_mode": "scripted",
        "tool_backend": "pack",
        "cases_dir": CASES,
        "knowledge_dir": RUNBOOKS,
        "embedding_mode": "fixture",
        "embedding_fixture": EMBEDDING_FIXTURE,
    }
    return Settings(**{**values, **overrides})  # type: ignore[arg-type]


async def test_every_case_is_evaluated_and_the_report_is_complete(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    report = await evaluate(_settings(pg_url), load_cases(CASES), queries=QUERIES)

    assert report.passed, [g for g in report.hard_gates if not g.passed]
    assert [c.case for c in report.cases] == ["s1-a", "s1-b", "s1-c", "s1-d", "s1-e", "s1-f"]
    assert all(c.status == "reported" for c in report.cases)
    assert report.summary.root_cause_correct.total == 6
    assert sum(len(c.followup) for c in report.cases) == 19
    assert all(q.outcome == "answered" for c in report.cases for q in c.followup)
    assert report.llm.mode == "scripted" and report.llm.note is None
    assert report.retrieval["metrics"]["mrr"] > 0
    assert set(report.prompts) >= {"plan.md", "report.md", "followup.md"}
    assert {g.name for g in report.hard_gates} == {
        "investigations_report",
        "outputs_pass_the_gate",
        "no_write_tools",
        "no_agent_evidence",
        "knowledge_not_cited",
    }

    write_report(tmp_path, report)
    document = json.loads((tmp_path / "report.json").read_text())
    jsonschema.validate(document, EvalReport.model_json_schema())
    assert EvalReport.model_validate(document) == report
    markdown = (tmp_path / "report.md").read_text()
    for label in (
        "root cause correct",
        "citation completeness (first report)",
        "required evidence coverage",
        "gate first pass: report",
        "follow-up cited segments",
        "follow-up unknown answered as unknown",
        "hit_at_5",
    ):
        assert label in markdown


async def test_the_evaluation_validates_with_the_runtime_gate(
    migrated: Engine, pg_url: str
) -> None:
    """One validator for the runtime and the evaluation; a bad report is caught."""
    assert eval_run.validate is validate  # type: ignore[attr-defined]

    settings = _settings(pg_url)
    [item] = load_cases(CASES, ["s1-a"])
    report = await evaluate(settings, [item], queries=QUERIES)
    assert report.passed
    engine = create_async_engine(pg_url, poolclass=NullPool)
    sessions = session_factory(engine)
    try:
        run = await eval_run._read_back(
            sessions, item, report.cases[0].investigation_id, [("no_answer", None)] * 4, 0.0
        )
        final = final_report(run)
        assert final is not None
        alert = case_alert(settings, "s1-a")
        assert await revalidate(settings, sessions, run, alert) == []

        cause = {**final.body["root_cause"], "citations": ["ev_0000000000"]}
        tampered = replace(
            run, reports=(ReportRow(1, "published", {**final.body, "root_cause": cause}),)
        )
        assert await revalidate(settings, sessions, tampered, alert) == [
            "s1-a: final report rejected by ['citation_integrity']"
        ]
    finally:
        await engine.dispose()


async def test_the_evaluation_needs_an_empty_database(migrated: Engine, pg_url: str) -> None:
    cases = load_cases(CASES, ["s1-f"])
    await evaluate(_settings(pg_url), cases, queries=QUERIES)
    with pytest.raises(EvaluationError, match="already holds investigations"):
        await evaluate(_settings(pg_url), cases, queries=QUERIES)


async def test_recording_needs_live_mode(pg_url: str, tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="live model mode"):
        await evaluate(_settings(pg_url), [], queries=QUERIES, record=tmp_path)


def test_unknown_cases_are_refused() -> None:
    with pytest.raises(EvaluationError, match="s1-z"):
        load_cases(CASES, ["s1-a", "s1-z"])


@pytest.fixture
async def listener() -> AsyncIterator[int]:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


async def test_the_network_probe(monkeypatch: pytest.MonkeyPatch, listener: int) -> None:
    monkeypatch.setattr(eval_run, "_PROBE_ADDRESS", ("127.0.0.1", listener))
    assert not await eval_run.network_isolated()
    monkeypatch.setattr(eval_run, "_PROBE_ADDRESS", ("127.0.0.1", 1))
    assert await eval_run.network_isolated()
