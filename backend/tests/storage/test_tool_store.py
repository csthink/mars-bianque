"""Tool calls write call records and evidence to PostgreSQL.

The executor runs real tools on the S1-a evidence pack and writes through the
database sink, so this covers the path an investigation and an MCP client take.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from bianque.redaction import build_engine
from bianque.storage.db import session_factory
from bianque.storage.models import CallRecord, Evidence
from bianque.storage.repositories import InvestigationRepository
from bianque.tools import CallerScope, InvestigationContext, ToolExecutor
from bianque.tools.backends.pack import open_pack
from bianque.tools.store import DatabaseToolCallSink

PACK = Path(__file__).resolve().parents[3] / "eval" / "cases" / "s1-a" / "pack"
POD_ARGS = {"namespace": "demo-s1", "name": "s1-app"}


async def test_investigation_calls_store_evidence_and_mcp_calls_do_not(
    migrated: Engine, pg_url: str
) -> None:
    engine = create_async_engine(pg_url, poolclass=NullPool)
    sessions = session_factory(engine)
    async with sessions() as session, session.begin():
        investigation = await InvestigationRepository(session).create(
            trigger="replay", tool_backend="pack", llm_mode="scripted", token_budget=1000
        )
    async with open_pack(PACK) as backends:
        executor = ToolExecutor(backends, DatabaseToolCallSink(sessions), engine=build_engine())
        context = InvestigationContext(investigation.id)
        agent = CallerScope(agent="kubernetes", domain="kubernetes")
        first = await executor.call(
            "k8s_get_deployment", POD_ARGS, scope=agent, investigation=context
        )
        again = await executor.call(
            "k8s_get_deployment", POD_ARGS, scope=agent, investigation=context
        )
        mcp = CallerScope(agent="mcp", caller="claude-code")
        outside = await executor.call("k8s_get_deployment", POD_ARGS, scope=mcp)
        denied = await executor.call("change_list_commits", {}, scope=agent, investigation=context)
    assert first.status == again.status == outside.status == "ok"
    assert denied.status == "rejected"
    assert first.evidence_ids == again.evidence_ids and len(first.evidence_ids) == 1
    assert outside.evidence_ids == ()

    async with sessions() as session:
        evidence = (await session.scalars(select(Evidence))).all()
        calls = (await session.scalars(select(CallRecord).order_by(CallRecord.id))).all()
    assert [(e.investigation_id, e.id, e.source, e.tool) for e in evidence] == [
        (investigation.id, first.evidence_ids[0], "kubernetes", "k8s_get_deployment")
    ]
    assert evidence[0].call_id == calls[0].id
    assert evidence[0].redacted_content == first.model_view.text
    assert "s1-app" in evidence[0].identifiers
    assert [(c.kind, c.agent, c.status, c.investigation_id, c.caller) for c in calls] == [
        ("tool", "kubernetes", "ok", investigation.id, None),
        ("tool", "kubernetes", "ok", investigation.id, None),
        ("tool", "mcp", "ok", None, "claude-code"),
        ("tool", "kubernetes", "rejected", investigation.id, None),
    ]
    for call in calls:
        assert call.name and call.input_summary and call.output_summary
        assert call.started_at is not None and call.latency_ms is not None
    assert calls[3].error == "rejected: change_list_commits is not a tool of the kubernetes domain"
    await engine.dispose()


async def test_evidence_cannot_come_from_a_model_or_an_agent(migrated: Engine, pg_url: str) -> None:
    engine = create_async_engine(pg_url, poolclass=NullPool)
    async with session_factory(engine)() as session:
        investigation = await InvestigationRepository(session).create(
            trigger="replay", tool_backend="pack", llm_mode="scripted", token_budget=1000
        )
        await session.commit()
        for source in ("model", "agent", "kubernetes-agent"):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "INSERT INTO evidence (investigation_id, id, source, collected_at, "
                            "raw_content, redacted_content) VALUES (:i, :e, :s, now(), 'x', 'x')"
                        ),
                        {"i": investigation.id, "e": f"ev_{source}", "s": source},
                    )
        count = await session.scalar(select(func.count()).select_from(Evidence))
    assert count == 0
    await engine.dispose()
