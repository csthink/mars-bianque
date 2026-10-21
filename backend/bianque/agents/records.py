"""Where an investigation writes its model call records and progress events.

``DatabaseCallRecorder`` is the gateway's ``CallRecorder``: one ``call_records`` row per
model call, and the call's tokens added to the investigation's total in the same
transaction, so the budget check after a round reads every completed call. The gate
result of a call is known only after the call, so it is written back afterwards. A
follow-up call carries a session and no investigation, and its tokens count for the
session only.

``EventWriter`` appends progress events. Each payload is validated against the model of
its event type first, so events carry IDs, statuses and validator names, never model
text; after the commit, readers in this process are woken. ``follow_events`` is the
reader: the investigation's event stream and the A2A task streams both follow an
investigation with it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.events import TERMINAL_EVENTS, EventNotifier, event_payload
from bianque.llm import CallContext, CallEvent
from bianque.storage.models import InvestigationEvent
from bianque.storage.repositories import (
    CallRecordRepository,
    EventRepository,
    InvestigationRepository,
)


class DatabaseCallRecorder:
    """Stores model calls in ``call_records`` and counts their tokens."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Use ``sessions`` to open one transaction per call."""
        self._sessions = sessions
        self._last: dict[tuple[str | None, int | None, str, str], int] = {}

    async def record(self, event: CallEvent) -> None:
        """Write the call record and add its tokens to the investigation."""
        async with self._sessions() as session, session.begin():
            record = await CallRecordRepository(session).add(
                agent=event.agent,
                kind=event.kind,
                name=event.name,
                input_summary=event.input_summary,
                output_summary=event.output_summary,
                status=event.status,
                started_at=event.started_at,
                investigation_id=event.investigation_id,
                session_id=event.session_id,
                error=event.error,
                retries=event.retries,
                latency_ms=event.latency_ms,
                tokens_in=event.tokens_in,
                tokens_out=event.tokens_out,
            )
            tokens = (event.tokens_in or 0) + (event.tokens_out or 0)
            if event.investigation_id is not None and tokens:
                await InvestigationRepository(session).add_tokens(event.investigation_id, tokens)
        key = (event.investigation_id, event.session_id, event.agent, event.name)
        self._last[key] = record.id

    async def set_gate_result(self, context: CallContext, gate_result: Mapping[str, Any]) -> None:
        """Attach a gate result to the latest call made with ``context``.

        The calls of one agent in one investigation or session are sequential, so the
        latest call at a call site is the one whose output was just validated, even while
        other sub-agents or other sessions run concurrently.
        """
        key = (context.investigation_id, context.session_id, context.agent, context.operation)
        record_id = self._last.get(key)
        if record_id is None:
            return
        async with self._sessions() as session, session.begin():
            await CallRecordRepository(session).set_gate_result(record_id, gate_result)


class EventWriter:
    """Appends progress events of investigations."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        notifier: EventNotifier | None = None,
    ) -> None:
        """Use ``sessions`` to open one transaction per event.

        Args:
            sessions: Database sessions.
            notifier: Wakes the event stream readers of this process; the command line
                has none.
        """
        self._sessions = sessions
        self._notifier = notifier

    async def emit(self, investigation_id: str, event_type: str, **payload: Any) -> None:
        """Validate and append one event, then wake the readers of the investigation."""
        values = event_payload(event_type, payload)
        async with self._sessions() as session, session.begin():
            await EventRepository(session).append(investigation_id, event_type, values)
        if self._notifier is not None:
            self._notifier.notify(investigation_id)


async def follow_events(
    sessions: async_sessionmaker[AsyncSession],
    notifier: EventNotifier,
    investigation_id: str,
    after: int,
    *,
    poll_seconds: float,
) -> AsyncIterator[InvestigationEvent | None]:
    """Yield the events of an investigation after ``after``, in order, as they are stored.

    The table is the only source of events; a wake-up only makes the next read sooner,
    and the read after each wait also finds events another process wrote. ``None`` is
    yielded after each read that found nothing new, so a reader can keep its connection
    alive. The iteration ends after ``investigation_finished`` or
    ``investigation_interrupted``.

    Each read uses a session of its own: one session held for the whole stream would
    stay idle in a transaction, which blocks statements that wait for every transaction,
    such as the ``CREATE INDEX CONCURRENTLY`` of the checkpoint tables' setup.
    """
    last = after
    while True:
        async with sessions() as session:
            rows = await EventRepository(session).list_after(investigation_id, last)
        for row in rows:
            last = row.seq
            yield row
            if row.type in TERMINAL_EVENTS:
                return
        if not rows:
            yield None
        await notifier.wait(investigation_id, poll_seconds)
