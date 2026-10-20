"""Investigations inside the backend process: start them, run them, interrupt them.

The API creates an investigation while it answers the request and runs it here, in a
background task of the same process (``docs/adr/0004-postgresql-only.md``, decision 3: one
replica). ``interrupt_unfinished`` runs at startup: an investigation that was pending or
running when the previous process ended is marked ``interrupted`` and is never resumed.
``seed_replay`` is the startup of the replay profile.

This module does not import FastAPI; the API maps its errors to responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.events import EventNotifier
from bianque.agents.records import EventWriter
from bianque.agents.runner import (
    Sleep,
    create_investigation,
    create_reinvestigation,
    execute_investigation,
    fail_investigation,
)
from bianque.api.alertmanager import AlertInput, parse_webhook
from bianque.config import Settings
from bianque.knowledge.embedding import build_embedder
from bianque.knowledge.ingest import ingest
from bianque.llm import ScriptedRule
from bianque.storage.repositories import InvestigationRepository, PlanRepository
from bianque.tools.backends import open_backends

logger = logging.getLogger(__name__)

#: A case name: a directory name under the cases directory, never a path.
CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class CaseNotFoundError(LookupError):
    """The case has no evidence pack."""


class PackBackendRequiredError(RuntimeError):
    """A case can only be replayed with the evidence pack backend."""


@dataclass(frozen=True)
class DeliveredAlert:
    """What became of one alert of a webhook delivery.

    Attributes:
        alert: The alert.
        investigation_id: The investigation started for it; ``None`` for a resolved
            alert and for a deduplicated one.
        deduplicated: An investigation of the same fingerprint had not ended yet.
    """

    alert: AlertInput
    investigation_id: str | None
    deduplicated: bool


class InvestigationService:
    """Creates investigations and runs them in background tasks of this process."""

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        notifier: EventNotifier,
        *,
        rules: Sequence[ScriptedRule] = (),
        tool_sleep: Sleep = asyncio.sleep,
        model_sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Run investigations with ``settings`` on the database of ``sessions``.

        Args:
            settings: Process settings.
            sessions: Database sessions.
            notifier: Wakes the event stream readers.
            rules: Scripted rules tried before the role rules; tests inject them.
            tool_sleep: Waits between tool retries.
            model_sleep: Waits between model retries.
        """
        self._settings = settings
        self._sessions = sessions
        self._notifier = notifier
        self._rules = tuple(rules)
        self._tool_sleep = tool_sleep
        self._model_sleep = model_sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def deliver(self, alerts: Sequence[AlertInput]) -> list[DeliveredAlert]:
        """Start an investigation for each firing alert that is not under investigation.

        A resolved alert starts nothing and is not stored. With the evidence pack
        backend, the tools read the pack of ``BIANQUE_PACK_CASE``.
        """
        case_id = self._settings.pack_case if self._settings.tool_backend == "pack" else None
        delivered = []
        for alert in alerts:
            if alert.status != "firing":
                delivered.append(DeliveredAlert(alert, None, False))
                continue
            created = await create_investigation(
                self._settings,
                alert,
                sessions=self._sessions,
                trigger="alert",
                case_id=case_id,
                deduplicate=True,
            )
            if created.investigation_id is not None:
                self._start(created.investigation_id, alert, case_id)
            delivered.append(DeliveredAlert(alert, created.investigation_id, created.deduplicated))
        return delivered

    async def replay_case(self, case_id: str) -> str:
        """Start a replay of a case's evidence pack and return the investigation ID.

        Raises:
            PackBackendRequiredError: The tool backend is not the evidence pack.
            CaseNotFoundError: The case has no evidence pack.
        """
        if self._settings.tool_backend != "pack":
            raise PackBackendRequiredError("a case is replayed with BIANQUE_TOOL_BACKEND=pack")
        alert = case_alert(self._settings, case_id)
        created = await create_investigation(
            self._settings, alert, sessions=self._sessions, trigger="replay", case_id=case_id
        )
        assert created.investigation_id is not None
        self._start(created.investigation_id, alert, case_id)
        return created.investigation_id

    async def reinvestigate(self, alert_id: int) -> str:
        """Start a manual investigation of an alert received earlier.

        Raises:
            NotFoundError: No such alert.
        """
        case_id = self._settings.pack_case if self._settings.tool_backend == "pack" else None
        investigation_id, alert = await create_reinvestigation(
            self._settings, alert_id, sessions=self._sessions, case_id=case_id
        )
        self._start(investigation_id, alert, case_id)
        return investigation_id

    async def wait(self, investigation_id: str) -> None:
        """Return when the background run of an investigation has ended."""
        task = self._tasks.get(investigation_id)
        if task is not None:
            await asyncio.shield(task)

    async def close(self) -> None:
        """Cancel every running investigation and wait for the tasks to end.

        The cancelled investigations keep their status; the next start marks them
        ``interrupted``.
        """
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start(self, investigation_id: str, alert: AlertInput, case_id: str | None) -> None:
        task = asyncio.create_task(self._run(investigation_id, alert, case_id))
        self._tasks[investigation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(investigation_id, None))

    async def _run(self, investigation_id: str, alert: AlertInput, case_id: str | None) -> None:
        try:
            async with open_backends(self._settings, case=case_id) as backends:
                await execute_investigation(
                    self._settings,
                    investigation_id,
                    alert,
                    sessions=self._sessions,
                    backends=backends,
                    rules=self._rules,
                    notifier=self._notifier,
                    tool_sleep=self._tool_sleep,
                    model_sleep=self._model_sleep,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The backends could not be opened, or the end of the run could not be
            # recorded; the investigation must not stay pending.
            try:
                await fail_investigation(
                    self._sessions,
                    EventWriter(self._sessions, self._notifier),
                    investigation_id,
                    exc,
                )
            except Exception:
                logger.exception("could not record the failure of %s", investigation_id)


def case_alert(settings: Settings, case_id: str) -> AlertInput:
    """Return the alert of a case's evidence pack.

    Raises:
        CaseNotFoundError: The case has no evidence pack.
    """
    path = settings.cases_dir / case_id / "pack" / "alert.json"
    if not CASE_ID.fullmatch(case_id) or not path.is_file():
        raise CaseNotFoundError(f"case {case_id!r} has no evidence pack")
    return parse_webhook(json.loads(path.read_text()))[0]


def pack_cases(settings: Settings) -> list[str]:
    """Return the cases that have an evidence pack, by name."""
    if not settings.cases_dir.is_dir():
        return []
    return sorted(path.parent.parent.name for path in settings.cases_dir.glob("*/pack/alert.json"))


async def interrupt_unfinished(
    sessions: async_sessionmaker[AsyncSession], notifier: EventNotifier | None = None
) -> list[str]:
    """Mark every pending or running investigation ``interrupted``; return their IDs.

    Called at startup, before any request is served: such an investigation belonged to
    a process that has ended. Its plans, steps, evidence and call records stay as they
    were; its unfinished steps are marked failed, since steps have no interrupted status.
    """
    events = EventWriter(sessions, notifier)
    async with sessions() as session:
        unfinished = list(await InvestigationRepository(session).list_unfinished())
    for investigation_id in unfinished:
        finished_at = datetime.now(UTC)
        async with sessions() as session, session.begin():
            await InvestigationRepository(session).set_status(
                investigation_id, "interrupted", finished_at=finished_at
            )
            await PlanRepository(session).fail_unfinished_steps(investigation_id, finished_at)
        await events.emit(investigation_id, "investigation_interrupted", status="interrupted")
        logger.warning("investigation %s was interrupted by a restart", investigation_id)
    return unfinished


async def seed_replay(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    service: InvestigationService,
) -> list[str]:
    """Import the runbooks, then replay each case that has no replay investigation yet.

    The cases run one after another, so that ``live`` mode does not send six
    investigations to the model service at once. A failed import is logged and the
    investigations still run; their retrieval degrades as it would without runbooks.

    Returns:
        The IDs of the investigations started.
    """
    try:
        summary = await ingest(
            settings.knowledge_dir, sessions=sessions, embedder=build_embedder(settings)
        )
        logger.info("replay startup imported the runbooks: %s", summary)
    except Exception:
        logger.exception("replay startup could not import %s", settings.knowledge_dir)
    started = []
    for case_id in pack_cases(settings):
        async with sessions() as session:
            if await InvestigationRepository(session).has_replay(case_id):
                continue
        investigation_id = await service.replay_case(case_id)
        started.append(investigation_id)
        await service.wait(investigation_id)
    return started
