"""Run one investigation: record the alert, assemble the dependencies, execute the graph.

Creating an investigation and executing it are two steps: the API creates it while the
request is answered and executes it in a background task; the command line does both
in one call (``run_investigation``). Every
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

from bianque.agents.events import EventNotifier
from bianque.agents.graph import RECURSION_LIMIT, build_graph
from bianque.agents.records import DatabaseCallRecorder, EventWriter
from bianque.agents.scripted_rules import ROLE_RULES
from bianque.agents.state import Runtime
from bianque.api.alertmanager import AlertInput, parse_alert
from bianque.config import Settings
from bianque.knowledge import KnowledgeRetriever
from bianque.knowledge.embedding import build_embedder
from bianque.llm import ScriptedRule, build_gateway, build_redaction_engine
from bianque.skills import SkillCatalog
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


@dataclass(frozen=True)
class CreatedInvestigation:
    """The result of recording an alert for investigation.

    Attributes:
        alert_id: The alert row: a new one, or the one already under investigation.
        investigation_id: The new pending investigation, or ``None`` when the alert was
            deduplicated.
    """

    alert_id: int
    investigation_id: str | None

    @property
    def deduplicated(self) -> bool:
        """``True`` when an investigation of the same fingerprint had not ended yet."""
        return self.investigation_id is None


async def create_investigation(
    settings: Settings,
    alert: AlertInput,
    *,
    sessions: async_sessionmaker[AsyncSession],
    trigger: str,
    case_id: str | None = None,
    deduplicate: bool = False,
) -> CreatedInvestigation:
    """Record an alert and create a pending investigation of it.

    Args:
        settings: Process settings: tool backend, model mode and budget.
        alert: The alert.
        sessions: Database sessions.
        trigger: ``alert``, ``manual`` or ``replay``.
        case_id: The evaluation case whose evidence pack the tools read.
        deduplicate: Create nothing when an investigation of the same fingerprint has
            not ended yet (webhook deliveries); otherwise always create one.
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
        if deduplicate and recorded.under_investigation:
            return CreatedInvestigation(recorded.alert.id, None)
        investigation_id = await _create(session, settings, recorded.alert.id, trigger, case_id)
    return CreatedInvestigation(recorded.alert.id, investigation_id)


async def create_reinvestigation(
    settings: Settings,
    alert_id: int,
    *,
    sessions: async_sessionmaker[AsyncSession],
    case_id: str | None = None,
) -> tuple[str, AlertInput]:
    """Create a pending manual investigation of an alert received earlier.

    Returns:
        The investigation ID and the alert as it was received.

    Raises:
        NotFoundError: No such alert.
    """
    async with sessions() as session, session.begin():
        alert = await AlertRepository(session).get(alert_id)
        investigation_id = await _create(session, settings, alert.id, "manual", case_id)
        return investigation_id, parse_alert(alert.payload)


async def _create(
    session: AsyncSession,
    settings: Settings,
    alert_id: int,
    trigger: str,
    case_id: str | None,
) -> str:
    investigation = await InvestigationRepository(session).create(
        trigger=trigger,
        tool_backend=settings.tool_backend,
        llm_mode=settings.llm_mode,
        llm_model=settings.llm_model or None,
        token_budget=settings.investigation_token_budget,
        alert_id=alert_id,
        case_id=case_id,
    )
    return investigation.id


async def fail_investigation(
    sessions: async_sessionmaker[AsyncSession],
    events: EventWriter,
    investigation_id: str,
    exc: BaseException,
) -> None:
    """Record that a run ended with ``exc``: the investigation and its open steps fail.

    The stored error names the exception class only: it is shown by the API, and an
    exception message may quote unredacted data. The log has the details.
    """
    logger.error("investigation %s failed", investigation_id, exc_info=exc)
    finished_at = datetime.now(UTC)
    async with sessions() as session, session.begin():
        await InvestigationRepository(session).set_status(
            investigation_id, "failed", finished_at=finished_at, error=type(exc).__name__
        )
        await PlanRepository(session).fail_unfinished_steps(investigation_id, finished_at)
    await events.emit(investigation_id, "investigation_finished", status="failed")


async def execute_investigation(
    settings: Settings,
    investigation_id: str,
    alert: AlertInput,
    *,
    sessions: async_sessionmaker[AsyncSession],
    backends: ToolBackends,
    skills: SkillCatalog,
    rules: Sequence[ScriptedRule] = (),
    notifier: EventNotifier | None = None,
    tool_sleep: Sleep = asyncio.sleep,
    model_sleep: Sleep = asyncio.sleep,
) -> InvestigationOutcome:
    """Run a pending investigation to the end.

    Args:
        settings: Process settings: model mode, limits, budget and database. The
            recordings directories are narrowed to the investigation's case.
        investigation_id: The pending investigation.
        alert: The alert under investigation.
        sessions: Database sessions.
        backends: The tool backends: the cluster and repository, or an evidence pack.
        skills: The skills the planning model may load, loaded at startup.
        rules: Scripted rules tried before the role rules; tests use them to inject
            failures. Only ``scripted`` mode reads them.
        notifier: Wakes the event stream readers of this process.
        tool_sleep: Waits between tool retries.
        model_sleep: Waits between model retries.

    Returns:
        The investigation ID and its final status.
    """
    async with sessions() as session:
        case_id = (await InvestigationRepository(session).get(investigation_id)).case_id
    settings = settings.for_case(case_id)
    engine = build_redaction_engine(settings)
    recorder = DatabaseCallRecorder(sessions)
    events = EventWriter(sessions, notifier)
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
        skills=skills,
        max_skill_activations=settings.plan_max_skill_activations,
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
        # A failed run leaves no step pending or running: the sub-agents of the round
        # were cancelled with it.
        await fail_investigation(sessions, events, investigation_id, exc)
        return InvestigationOutcome(investigation_id, "failed")
    async with sessions() as session:
        status = (await InvestigationRepository(session).get(investigation_id)).status
    return InvestigationOutcome(investigation_id, status)


async def run_investigation(
    settings: Settings,
    alert: AlertInput,
    *,
    sessions: async_sessionmaker[AsyncSession],
    backends: ToolBackends,
    skills: SkillCatalog,
    trigger: str,
    case_id: str | None = None,
    rules: Sequence[ScriptedRule] = (),
    tool_sleep: Sleep = asyncio.sleep,
    model_sleep: Sleep = asyncio.sleep,
) -> InvestigationOutcome:
    """Investigate one alert to the end: create the investigation, then execute it.

    The command line uses this; the API creates and executes in two steps, so that the
    response can carry the investigation ID while the run continues in the background.
    The arguments are those of ``create_investigation`` and ``execute_investigation``.

    Returns:
        The investigation ID and its final status.
    """
    created = await create_investigation(
        settings, alert, sessions=sessions, trigger=trigger, case_id=case_id
    )
    assert created.investigation_id is not None
    return await execute_investigation(
        settings,
        created.investigation_id,
        alert,
        sessions=sessions,
        backends=backends,
        skills=skills,
        rules=rules,
        tool_sleep=tool_sleep,
        model_sleep=model_sleep,
    )
