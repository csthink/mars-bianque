"""Run investigations of the S1 evidence packs in ``scripted`` mode, and read them back.

Tests run the real graph, the real tools on an evidence pack, the real evidence gate and
a real PostgreSQL; only the model is replaced by the scripted role rules. A test injects
rules before the role rules to make one call site misbehave, and may wrap the tool
backends to delay or synchronize calls.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bianque.agents.runner import InvestigationOutcome, run_investigation
from bianque.api.alertmanager import AlertInput, parse_webhook
from bianque.config import Settings
from bianque.llm import ScriptedRule
from bianque.storage.db import session_factory
from bianque.storage.models import (
    Assessment,
    CallRecord,
    Evidence,
    Investigation,
    InvestigationEvent,
    Plan,
    Report,
    Step,
)
from bianque.tools.backends.base import GitSource, KubernetesSource, ToolBackends
from bianque.tools.backends.pack import open_pack

#: Root of the evaluation cases, where the committed evidence packs live.
CASES = Path(__file__).resolve().parents[3] / "eval" / "cases"


async def no_wait(_: float) -> None:
    """A sleep that returns at once, for retries in tests."""


def case_alert(case: str) -> AlertInput:
    """Return the alert of a case's evidence pack."""
    return parse_webhook(json.loads((CASES / case / "pack" / "alert.json").read_text()))[0]


def scripted_settings(pg_url: str, **overrides: Any) -> Settings:
    """Settings of a scripted investigation on the test database."""
    values: dict[str, Any] = {
        "database_url": pg_url,
        "llm_mode": "scripted",
        "tool_backend": "pack",
        "cases_dir": CASES,
    }
    return Settings(**{**values, **overrides})


@dataclass
class Investigated:
    """A finished investigation and the rows it wrote."""

    outcome: InvestigationOutcome
    investigation: Investigation
    plans: list[Plan]
    steps: list[Step]
    assessments: list[Assessment]
    evidence: list[Evidence]
    calls: list[CallRecord]
    reports: list[Report]
    events: list[InvestigationEvent]

    @property
    def final(self) -> Report | None:
        """The published or degraded report."""
        return next((r for r in self.reports if r.status in ("published", "degraded")), None)

    def calls_of(self, agent: str, kind: str | None = None) -> list[CallRecord]:
        """The calls of one agent, optionally of one kind."""
        return [c for c in self.calls if c.agent == agent and (kind is None or c.kind == kind)]


async def read_back(
    sessions: async_sessionmaker[AsyncSession], outcome: InvestigationOutcome
) -> Investigated:
    """Read every row an investigation wrote."""
    inv = outcome.investigation_id
    async with sessions() as session:

        async def rows(model: Any, *order: Any) -> list[Any]:
            result = await session.scalars(
                select(model).where(model.investigation_id == inv).order_by(*order)
            )
            return list(result.all())

        return Investigated(
            outcome=outcome,
            investigation=await session.get_one(Investigation, inv),
            plans=await rows(Plan, Plan.version),
            steps=await rows(Step, Step.plan_id, Step.seq),
            assessments=await rows(Assessment, Assessment.round),
            evidence=await rows(Evidence, Evidence.id),
            calls=await rows(CallRecord, CallRecord.started_at, CallRecord.id),
            reports=await rows(Report, Report.attempt),
            events=await rows(InvestigationEvent, InvestigationEvent.seq),
        )


@asynccontextmanager
async def database(pg_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on the test database."""
    engine = create_async_engine(pg_url, poolclass=NullPool)
    try:
        yield session_factory(engine)
    finally:
        await engine.dispose()


async def investigate(
    pg_url: str,
    case: str = "s1-a",
    *,
    rules: Sequence[ScriptedRule] = (),
    wrap: Callable[[ToolBackends], ToolBackends] | None = None,
    **overrides: Any,
) -> Investigated:
    """Investigate the alert of a case's evidence pack and read the result back.

    Args:
        pg_url: The migrated test database.
        case: The case whose evidence pack the tools read.
        rules: Scripted rules tried before the role rules.
        wrap: Replaces the backends, for example to delay calls.
        overrides: Settings, such as ``subagent_max_steps``.
    """
    settings = scripted_settings(pg_url, **overrides)
    async with database(pg_url) as sessions, open_pack(CASES / case / "pack") as backends:
        outcome = await run_investigation(
            settings,
            case_alert(case),
            sessions=sessions,
            backends=wrap(backends) if wrap else backends,
            trigger="replay",
            case_id=case,
            rules=rules,
            tool_sleep=no_wait,
            model_sleep=no_wait,
        )
        return await read_back(sessions, outcome)


Hook = Callable[[str], Awaitable[None]]


class _Hooked:
    """Delegates every method of a source, awaiting a hook with the method name first."""

    def __init__(self, inner: object, hook: Hook) -> None:
        self._inner = inner
        self._hook = hook

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            await self._hook(name)
            return await method(*args, **kwargs)

        return call


def hooked(hook: Hook) -> Callable[[ToolBackends], ToolBackends]:
    """Return a backend wrapper that awaits ``hook(method)`` before every source call."""

    def wrap(backends: ToolBackends) -> ToolBackends:
        return replace(
            backends,
            kubernetes=cast(KubernetesSource, _Hooked(backends.kubernetes, hook)),
            git=cast(GitSource, _Hooked(backends.git, hook)),
        )

    return wrap
