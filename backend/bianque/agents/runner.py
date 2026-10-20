"""Run one investigation: assemble the dependencies, execute the graph, record the end.

The command line uses this module, and the API's background tasks will as well. Every
run checkpoints the graph state in PostgreSQL under the investigation ID. The checkpoint
library has no schema parameter, so it gets its own connection pool whose
``search_path`` is the ``langgraph`` schema; migrations leave that schema
alone. A run is never resumed from its checkpoint.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.graph import RECURSION_LIMIT, build_graph
from bianque.agents.records import DatabaseCallRecorder, EventWriter
from bianque.agents.scripted_rules import ROLE_RULES
from bianque.agents.state import Runtime
from bianque.api.alertmanager import AlertInput
from bianque.config import Settings
from bianque.knowledge import KnowledgeRetriever
from bianque.knowledge.embedding import build_embedder
from bianque.llm import ScriptedRule, build_gateway, build_redaction_engine
from bianque.storage.db import database_url
from bianque.storage.repositories import (
    AlertRepository,
    InvestigationRepository,
    PlanRepository,
)
from bianque.tools import ToolExecutor, registry
from bianque.tools.backends.base import ToolBackends
from bianque.tools.store import DatabaseToolCallSink

logger = logging.getLogger(__name__)

#: The schema of the checkpoint tables.
CHECKPOINT_SCHEMA = "langgraph"

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class InvestigationOutcome:
    """How a run ended.

    Attributes:
        investigation_id: The investigation.
        status: ``reported``, ``degraded`` or ``failed``.
    """

    investigation_id: str
    status: str


def checkpoint_conninfo(settings: Settings) -> str:
    """Return the libpq connection string of the checkpoint pool."""
    url = database_url(settings).replace("postgresql+psycopg://", "postgresql://", 1)
    return make_conninfo(url, options=f"-c search_path={CHECKPOINT_SCHEMA}")


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """Open the checkpoint store on its own pool and create its tables if needed."""
    async with AsyncConnectionPool(
        checkpoint_conninfo(settings),
        min_size=1,
        max_size=4,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as pool:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        yield saver


async def run_investigation(
    settings: Settings,
    alert: AlertInput,
    *,
    sessions: async_sessionmaker[AsyncSession],
    backends: ToolBackends,
    trigger: str,
    case_id: str | None = None,
    rules: Sequence[ScriptedRule] = (),
    tool_sleep: Sleep = asyncio.sleep,
    model_sleep: Sleep = asyncio.sleep,
) -> InvestigationOutcome:
    """Investigate one alert to the end.

    Args:
        settings: Process settings: model mode, limits, budget and database.
        alert: The alert to investigate.
        sessions: Database sessions.
        backends: The tool backends: the cluster and repository, or an evidence pack.
        trigger: ``alert``, ``manual`` or ``replay``.
        case_id: The evaluation case of a replay.
        rules: Scripted rules tried before the role rules; tests use them to inject
            failures. Only ``scripted`` mode reads them.
        tool_sleep: Waits between tool retries.
        model_sleep: Waits between model retries.

    Returns:
        The investigation ID and its final status.
    """
    async with sessions() as session, session.begin():
        recorded = await AlertRepository(session).record(
            fingerprint=alert.fingerprint,
            status=alert.status,
            labels=alert.labels,
            annotations=alert.annotations,
            starts_at=alert.starts_at,
            payload=alert.payload,
        )
        investigation = await InvestigationRepository(session).create(
            trigger=trigger,
            tool_backend=backends.kind,
            llm_mode=settings.llm_mode,
            llm_model=settings.llm_model or None,
            token_budget=settings.investigation_token_budget,
            alert_id=recorded.alert.id,
            case_id=case_id,
        )
    investigation_id = investigation.id
    engine = build_redaction_engine(settings)
    recorder = DatabaseCallRecorder(sessions)
    events = EventWriter(sessions)
    runtime = Runtime(
        sessions=sessions,
        gateway=build_gateway(
            settings, recorder=recorder, scripted_rules=[*rules, *ROLE_RULES], sleep=model_sleep
        ),
        recorder=recorder,
        executor=ToolExecutor(
            backends, DatabaseToolCallSink(sessions), engine=engine, sleep=tool_sleep
        ),
        tools=registry,
        engine=engine,
        events=events,
        knowledge=KnowledgeRetriever(sessions, build_embedder(settings)),
        alert=alert,
        subagent_max_steps=settings.subagent_max_steps,
        max_replan_rounds=settings.max_replan_rounds,
        report_max_retries=settings.report_max_retries,
        token_budget=settings.investigation_token_budget,
    )
    try:
        async with open_checkpointer(settings) as checkpointer:
            graph = build_graph(runtime).compile(checkpointer=checkpointer)
            await graph.ainvoke(
                {"investigation_id": investigation_id},
                config={
                    "configurable": {"thread_id": investigation_id},
                    "recursion_limit": RECURSION_LIMIT,
                },
            )
    except Exception as exc:
        # The stored error names the exception class only: it is shown by the API, and
        # an exception message may quote unredacted data. The log has the details.
        logger.exception("investigation %s failed", investigation_id)
        # A failed run leaves no step pending or running: the sub-agents of the round
        # were cancelled with it.
        finished_at = datetime.now(UTC)
        async with sessions() as session, session.begin():
            await InvestigationRepository(session).set_status(
                investigation_id, "failed", finished_at=finished_at, error=type(exc).__name__
            )
            await PlanRepository(session).fail_unfinished_steps(investigation_id, finished_at)
        await events.emit(investigation_id, "investigation_finished", status="failed")
        return InvestigationOutcome(investigation_id, "failed")
    async with sessions() as session:
        status = (await InvestigationRepository(session).get(investigation_id)).status
    return InvestigationOutcome(investigation_id, status)
