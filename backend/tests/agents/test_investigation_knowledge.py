"""Knowledge in a scripted S1-a investigation.

The investigation retrieves runbook chunks for the alert, gives them to the plan with
their texts, records the retrieval as a knowledge call, and the report refers to
knowledge only under references. Knowledge never becomes evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from investigation_harness import investigate
from sqlalchemy import Engine

from bianque.knowledge.search import KnowledgeRetriever, SearchResult
from bianque.llm import ScriptedReply
from bianque.llm.request import ModelRequest

KNOWLEDGE_ID = re.compile(r"kn_[0-9a-f]{12}")


def _knowledge_section(request: ModelRequest) -> str:
    user = next(m.content.text for m in request.prompt.messages if m.role == "user")
    return user.split("## Knowledge\n", 1)[1].split("\n## ", 1)[0]


async def test_the_plan_gets_retrieved_knowledge_and_the_report_refers_to_it(
    migrated: Engine, pg_url: str
) -> None:
    plans: list[ModelRequest] = []
    reports: list[ModelRequest] = []

    def capture(request: ModelRequest) -> ScriptedReply | None:
        if request.operation in ("plan", "replan"):
            plans.append(request)
        if request.operation == "report":
            reports.append(request)
        return None

    result = await investigate(pg_url, "s1-a", rules=[capture])
    assert result.outcome.status == "reported"

    (record,) = result.calls_of("investigation", "knowledge")
    assert record.status == "ok" and record.name == "retrieve_knowledge"
    assert record.input_summary.startswith("Bianque Pod Restarting\n\n")
    lines = (record.output_summary or "").splitlines()
    assert len(lines) == 5 and not lines[0].startswith("degraded")
    retrieved = [line.split(" ", 1)[0] for line in lines]
    assert all(KNOWLEDGE_ID.fullmatch(kid) for kid in retrieved)

    # The plan and the replan see the five chunks with their texts, sorted by ID.
    assert len(plans) == 2
    for request in plans:
        section = _knowledge_section(request)
        entries = re.findall(r"^\[(kn_[0-9a-f]{12})\] ", section, flags=re.MULTILINE)
        assert entries == sorted(retrieved)
        assert not any(line.startswith("## ") for line in section.splitlines())
        assert "\n  " in section
    # The report sees IDs and titles only.
    report_section = _knowledge_section(reports[0])
    assert re.findall(r"^- (kn_[0-9a-f]{12}): ", report_section, flags=re.MULTILINE) == sorted(
        retrieved
    )
    assert "\n  " not in report_section

    plan_calls = [c for c in result.calls_of("investigation", "model") if c.name == "plan"]
    assert min(retrieved) in (plan_calls[0].output_summary or "")
    assert plan_calls[0].gate_result and plan_calls[0].gate_result["passed"]

    assert result.final is not None and result.final.status == "published"
    body = result.final.body
    assert [ref["knowledge_id"] for ref in body["references"]] == [min(retrieved)]
    cited = [c for item in body["claims"] for c in item["citations"]]
    cited += body["root_cause"]["citations"]
    assert not any(KNOWLEDGE_ID.search(c) for c in cited)

    # Knowledge is not evidence and does not go through the tool executor.
    assert not any(KNOWLEDGE_ID.search(e.raw_content) for e in result.evidence)
    assert all(c.name != "knowledge_search" for c in result.calls)


async def test_without_the_fixture_retrieval_degrades_and_the_investigation_goes_on(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    result = await investigate(pg_url, "s1-a", embedding_fixture=tmp_path / "absent.json")
    assert result.outcome.status == "reported"
    (record,) = result.calls_of("investigation", "knowledge")
    assert record.status == "ok"
    assert (record.output_summary or "").startswith("degraded: vector_unavailable\n")


async def test_a_failing_retrieval_leaves_the_investigation_without_knowledge(
    migrated: Engine, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: KnowledgeRetriever, query: str, limit: int = 5) -> SearchResult:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(KnowledgeRetriever, "search", broken)
    result = await investigate(pg_url, "s1-a")
    assert result.outcome.status == "reported"
    (record,) = result.calls_of("investigation", "knowledge")
    assert (record.status, record.error, record.output_summary) == (
        "error",
        "ConnectionError",
        None,
    )
    assert result.final is not None and result.final.body["references"] == []


async def test_without_runbooks_the_investigation_has_no_knowledge(
    migrated: Engine, pg_url: str
) -> None:
    result = await investigate(pg_url, "s1-a", knowledge=False)
    assert result.outcome.status == "reported"
    (record,) = result.calls_of("investigation", "knowledge")
    assert record.output_summary == "(no knowledge found)"
