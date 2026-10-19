"""Where the executor writes call records and evidence.

The executor reports each call through ``ToolCallSink``, the same way the model gateway
reports to its ``CallRecorder``. ``DatabaseToolCallSink`` writes one transaction per
call: the call record first, then the evidence pointing at it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.storage.repositories import CallRecordRepository, EvidenceRepository


@dataclass(frozen=True)
class ToolCallRecord:
    """One tool call, as stored in ``call_records``."""

    agent: str
    name: str
    input_summary: str
    status: str
    started_at: datetime
    retries: int
    latency_ms: int
    investigation_id: str | None = None
    caller: str | None = None
    output_summary: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    """Evidence collected by a successful evidence tool call during an investigation."""

    investigation_id: str
    evidence_id: str
    source: str
    tool: str
    collected_at: datetime
    raw_content: str
    redacted_content: str
    identifiers: Sequence[str] = ()
    step_id: int | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


class ToolCallSink(Protocol):
    """Receives one call record, and possibly one piece of evidence, per tool call."""

    async def save(self, call: ToolCallRecord, evidence: EvidenceRecord | None) -> None:
        """Store the call and its evidence together."""
        ...


class DatabaseToolCallSink:
    """Stores tool calls and evidence in PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Use ``sessions`` to open one transaction per call."""
        self._sessions = sessions

    async def save(self, call: ToolCallRecord, evidence: EvidenceRecord | None) -> None:
        """Write the call record, then the evidence with the record's ID."""
        async with self._sessions() as session, session.begin():
            record = await CallRecordRepository(session).add(
                agent=call.agent,
                kind="tool",
                name=call.name,
                input_summary=call.input_summary,
                status=call.status,
                started_at=call.started_at,
                investigation_id=call.investigation_id,
                caller=call.caller,
                output_summary=call.output_summary,
                error=call.error,
                retries=call.retries,
                latency_ms=call.latency_ms,
            )
            if evidence is not None:
                await EvidenceRepository(session).add(
                    evidence.investigation_id,
                    evidence_id=evidence.evidence_id,
                    source=evidence.source,
                    collected_at=evidence.collected_at,
                    raw_content=evidence.raw_content,
                    redacted_content=evidence.redacted_content,
                    tool=evidence.tool,
                    step_id=evidence.step_id,
                    call_id=record.id,
                    identifiers=evidence.identifiers,
                    meta=evidence.meta,
                )
