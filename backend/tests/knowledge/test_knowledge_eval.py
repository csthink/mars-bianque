"""Retrieval evaluation: metrics, query files and the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from knowledge_fakes import FIXTURE, QUERIES, RUNBOOKS
from sqlalchemy import Engine

from bianque.cli import main
from bianque.evals.retrieval import (
    QueryOutcome,
    RetrievalEvalError,
    RetrievalQuery,
    evaluate,
    load_queries,
    metrics,
)
from bianque.knowledge.query import alert_query, split_words
from bianque.knowledge.search import KnowledgeHit, SearchResult


def test_metrics_follow_their_definitions() -> None:
    outcomes = [
        QueryOutcome("a", [], 1),
        QueryOutcome("b", [], 2),
        QueryOutcome("c", [], 4),
        QueryOutcome("d", [], None),
    ]
    assert metrics(outcomes) == {
        "hit_at_1": 0.25,
        "hit_at_3": 0.5,
        "hit_at_5": 0.75,
        "mrr": (1 + 1 / 2 + 1 / 4) / 4,
    }
    with pytest.raises(RetrievalEvalError):
        metrics([])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version: 2\nqueries: []\n", "schema_version is 2, expected 1"),
        ("queries: []\n", "schema_version is None"),
        ("schema_version: 1\nqueries:\n  - {id: a}\n", "malformed query"),
        (
            "schema_version: 1\nqueries:\n"
            "  - {id: a, query: x, expected: [kn_1]}\n  - {id: a, query: y, expected: [kn_1]}\n",
            "query IDs repeat",
        ),
        ("schema_version: 1\nqueries:\n  - {id: a, query: x, expected: []}\n", "at least one"),
    ],
)
def test_a_query_file_that_does_not_fit_fails_and_names_the_file(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "queries.yaml"
    path.write_text(text)
    with pytest.raises(RetrievalEvalError, match=f"{path}: .*{message}"):
        load_queries(path)


class _Retriever:
    def __init__(self, degraded: bool) -> None:
        self.degraded = degraded

    async def search(self, query: str, limit: int = 5) -> SearchResult:
        hit = KnowledgeHit("kn_b", "p.md", "T", "c", None, 1, 1 / 61)
        return SearchResult([hit], "vector_unavailable" if self.degraded else None)


async def test_a_degraded_retrieval_fails_the_evaluation() -> None:
    queries = [RetrievalQuery("q1", "x", ("kn_a", "kn_b"))]
    assert await evaluate(queries, _Retriever(False)) == [QueryOutcome("q1", ["kn_b"], 1)]
    with pytest.raises(RetrievalEvalError, match="query q1: retrieval degraded"):
        await evaluate(queries, _Retriever(True))


def test_the_alert_query_is_the_alert_name_in_words_and_the_annotations() -> None:
    assert split_words("BianquePodRestarting") == "Bianque Pod Restarting"
    assert split_words("PodOOMKilled") == "Pod OOM Killed"
    assert split_words("kube_pod_crash-looping") == "kube pod crash looping"
    query = alert_query(
        {"alertname": "BianquePodRestarting", "pod": "s1-app-x", "namespace": "demo-s1"},
        {"summary": "keeps restarting", "description": "CrashLoopBackOff"},
    )
    assert query == "Bianque Pod Restarting\n\nCrashLoopBackOff\n\nkeeps restarting"


def test_the_command_line_imports_and_evaluates_with_the_fixture(
    migrated: Engine, pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIANQUE_DATABASE_URL", pg_url)
    monkeypatch.setenv("BIANQUE_EMBEDDING_MODE", "fixture")
    monkeypatch.setenv("BIANQUE_EMBEDDING_FIXTURE", str(FIXTURE))
    assert main(["knowledge", "ingest", str(RUNBOOKS)]) == 0
    out = tmp_path / "report"
    assert main(["eval", "retrieval", "--queries", str(QUERIES), "--out", str(out)]) == 0
    report = json.loads((out / "retrieval.json").read_text())
    assert set(report["metrics"]) == {"hit_at_1", "hit_at_3", "hit_at_5", "mrr"}
    assert report["embedding_mode"] == "fixture"
    assert report["queries"] == len(load_queries(QUERIES)) == len(report["outcomes"])
    assert report["fixture_file"] == FIXTURE.name and len(report["fixture_sha256"]) == 64
    assert "| hit_at_5 |" in (out / "retrieval.md").read_text()


def test_the_command_line_refuses_stale_annotations(
    migrated: Engine,
    pg_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BIANQUE_DATABASE_URL", pg_url)
    monkeypatch.setenv("BIANQUE_EMBEDDING_FIXTURE", str(FIXTURE))
    assert main(["knowledge", "ingest", str(RUNBOOKS)]) == 0
    queries = tmp_path / "queries.yaml"
    queries.write_text(
        "schema_version: 1\nqueries:\n  - {id: q1, query: exit code, expected: [kn_000000000000]}\n"
    )
    assert main(["eval", "retrieval", "--queries", str(queries), "--out", str(tmp_path)]) == 1
    assert "annotated IDs not in the knowledge base: ['kn_000000000000']" in capsys.readouterr().err


def test_embed_fixtures_check_reports_an_outdated_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runbooks = tmp_path / "runbooks"
    runbooks.mkdir()
    (runbooks / "new.md").write_text("# New\n\n## Section\n\nText no vector covers.\n")
    args = ["knowledge", "embed-fixtures", "--check", "--runbooks", str(runbooks)]
    args += ["--cases", str(tmp_path), "--queries", str(QUERIES), "--out", str(FIXTURE)]
    assert main(args) == 1
    err = capsys.readouterr().err
    assert "missing: 'New > Section" in err and "out of date" in err
