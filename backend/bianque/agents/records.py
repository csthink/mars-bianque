"""Where an investigation writes its model call records and progress events.

``DatabaseCallRecorder`` is the gateway's ``CallRecorder``: one ``call_records`` row per
model call, and the call's tokens added to the investigation's total in the same
transaction, so the budget check after a round reads every completed call. The gate
result of a call is known only after the call, so it is written back afterwards. A
follow-up call carries a session and no investigation, and its tokens count for the
session only.

``EventWriter`` appends progress events. Each payload is validated against the model of
its event type first, so events carry IDs, statuses and validator names, never model
text; after the commit, readers in this process are woken.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.events import EventNotifier, event_payload
from bianque.llm import CallContext, CallEvent
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
