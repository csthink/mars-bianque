"""The auxiliary tool ``knowledge_search``: knowledge, never evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from tool_fakes import MemorySink, backends, no_sleep

from bianque.knowledge.search import KnowledgeHit, SearchResult
from bianque.redaction import build_engine
from bianque.tools import CallerScope, InvestigationContext, ToolExecutor, registry

MCP = CallerScope(agent="mcp", tools=frozenset({"knowledge_search"}), caller="claude-code")


class _Knowledge:
    def __init__(self, degraded: bool = False) -> None:
        self.queries: list[tuple[str, int]] = []
        self.degraded = degraded

    async def search(self, query: str, limit: int = 5) -> SearchResult:
        self.queries.append((query, limit))
        hits: Sequence[KnowledgeHit] = [
            KnowledgeHit(
                knowledge_id="kn_c73d8439117f",
                path="crashloopbackoff.md",
                heading_path="CrashLoopBackOff > Reading the previous container log",
                content="Read it with --previous.",
                vector_rank=None if self.degraded else 1,
                text_rank=2,
                score=1 / 62 if self.degraded else 1 / 61 + 1 / 62,
            )
        ]
        return SearchResult(list(hits), "vector_unavailable" if self.degraded else None)


def _executor(knowledge: _Knowledge | None, sink: MemorySink) -> ToolExecutor:
    return ToolExecutor(
        replace(backends(), knowledge=knowledge),
        sink,
        tools=registry,
        engine=build_engine(),
        sleep=no_sleep,
    )


async def test_the_search_returns_fused_results_and_stores_no_evidence() -> None:
    knowledge, sink = _Knowledge(), MemorySink()
    result = await _executor(knowledge, sink).call(
        "knowledge_search",
        {"query": "previous container log", "limit": 3},
        scope=MCP,
        investigation=InvestigationContext("inv_01TEST"),
    )
    assert result.status == "ok" and result.evidence_ids == ()
    assert knowledge.queries == [("previous container log", 3)]
    data = json.loads(result.model_view.text)
    assert data == {
        "degraded": None,
        "results": [
            {
                "knowledge_id": "kn_c73d8439117f",
                "path": "crashloopbackoff.md",
                "heading_path": "CrashLoopBackOff > Reading the previous container log",
                "vector_rank": 1,
                "text_rank": 2,
                "score": round(1 / 61 + 1 / 62, 6),
                "content": "Read it with --previous.",
            }
        ],
    }
    assert sink.evidence == []
    assert [(c.agent, c.name, c.status, c.caller) for c in sink.calls] == [
        ("mcp", "knowledge_search", "ok", "claude-code")
    ]


async def test_a_degraded_search_says_so() -> None:
    result = await _executor(_Knowledge(degraded=True), MemorySink()).call(
        "knowledge_search", {"query": "exit code"}, scope=MCP
    )
    data = json.loads(result.model_view.text)
    assert data["degraded"] == "vector_unavailable"
    assert data["results"][0]["vector_rank"] is None


async def test_without_a_knowledge_source_the_call_fails() -> None:
    sink = MemorySink()
    result = await _executor(None, sink).call("knowledge_search", {"query": "x"}, scope=MCP)
    assert (result.status, result.error) == ("error", "failed: knowledge base")
    assert [c.status for c in sink.calls] == ["error"]


async def test_the_arguments_are_bounded() -> None:
    executor = _executor(_Knowledge(), MemorySink())
    for arguments in ({"query": ""}, {"query": "x" * 501}, {"query": "x", "limit": 11}):
        result = await executor.call("knowledge_search", arguments, scope=MCP)
        assert result.status == "rejected", arguments


async def test_a_sub_agent_cannot_search_knowledge() -> None:
    result = await _executor(_Knowledge(), MemorySink()).call(
        "knowledge_search", {"query": "x"}, scope=CallerScope(agent="logs", domain="logs")
    )
    assert result.status == "rejected"
