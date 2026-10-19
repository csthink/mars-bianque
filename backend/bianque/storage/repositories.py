"""Repositories: the only code that reads and writes the tables.

Each repository holds one ``AsyncSession`` and works on one group of tables. It
flushes so that generated keys are available, but never commits: the caller owns the
transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bianque.storage import ids
from bianque.storage.errors import AlreadyDecidedError, NotFoundError
from bianque.storage.models import (
    OPEN_INVESTIGATION_STATUSES,
    Alert,
    Assessment,
    CallRecord,
    Confirmation,
    Evidence,
    FollowupMessage,
    FollowupSession,
    Investigation,
    InvestigationEvent,
    KnowledgeChunk,
    KnowledgeDocument,
    Plan,
    Report,
    Step,
    Suggestion,
)

#: Call summaries are cut to this many characters before they are stored.
SUMMARY_LIMIT = 2000


def truncate_summary(text: str | None) -> str | None:
    """Cut a call summary to ``SUMMARY_LIMIT`` characters.

    Args:
        text: An already redacted summary.

    Returns:
        The summary, at most ``SUMMARY_LIMIT`` characters long.
    """
    if text is None:
        return None
    return text[:SUMMARY_LIMIT]


async def _next_value(session: AsyncSession, column: Any, *where: Any) -> int:
    """Return one more than the largest value of ``column`` among matching rows."""
    current = await session.scalar(select(func.max(column)).where(*where))
    return 1 if current is None else int(current) + 1


@dataclass(frozen=True)
class RecordedAlert:
    """The result of recording an incoming alert."""

    alert: Alert
    #: ``True`` when an investigation of the same fingerprint has not ended yet, so the
    #: alert only refreshed ``received_at`` and must not start a new investigation.
    under_investigation: bool


class AlertRepository:
    """Alerts received from Alertmanager."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def record(
        self,
        *,
        fingerprint: str,
        status: str,
        labels: Mapping[str, Any],
        annotations: Mapping[str, Any],
        starts_at: datetime,
        payload: Mapping[str, Any],
    ) -> RecordedAlert:
        """Record an alert, or refresh the one already under investigation.

        When an alert with the same fingerprint has an investigation that has not ended,
        only that alert's ``received_at`` is updated; otherwise a new alert row is added.

        Returns:
            The alert row and whether it is already under investigation.
        """
        open_alert = await self._session.scalar(
            select(Alert)
            .join(Investigation, Investigation.alert_id == Alert.id)
            .where(
                Alert.fingerprint == fingerprint,
                Investigation.status.in_(OPEN_INVESTIGATION_STATUSES),
            )
            .order_by(Alert.id.desc())
            .limit(1)
        )
        if open_alert is not None:
            open_alert.received_at = func.now()
            await self._session.flush()
            await self._session.refresh(open_alert, ["received_at"])
            return RecordedAlert(alert=open_alert, under_investigation=True)
        alert = Alert(
            fingerprint=fingerprint,
            status=status,
            labels=dict(labels),
            annotations=dict(annotations),
            starts_at=starts_at,
            payload=dict(payload),
        )
        self._session.add(alert)
        await self._session.flush()
        await self._session.refresh(alert, ["received_at"])
        return RecordedAlert(alert=alert, under_investigation=False)

    async def get(self, alert_id: int) -> Alert:
        """Return an alert by ID.

        Raises:
            NotFoundError: No such alert.
        """
        alert = await self._session.get(Alert, alert_id)
        if alert is None:
            raise NotFoundError(f"alert {alert_id} not found")
        return alert


class InvestigationRepository:
    """Investigations."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create(
        self,
        *,
        trigger: str,
        tool_backend: str,
        llm_mode: str,
        token_budget: int,
        alert_id: int | None = None,
        case_id: str | None = None,
        llm_model: str | None = None,
    ) -> Investigation:
        """Create a pending investigation with a new ``inv_`` ID."""
        investigation = Investigation(
            id=ids.new_investigation_id(),
            alert_id=alert_id,
            trigger=trigger,
            case_id=case_id,
            tool_backend=tool_backend,
            llm_mode=llm_mode,
            llm_model=llm_model,
            token_budget=token_budget,
        )
        self._session.add(investigation)
        await self._session.flush()
        await self._session.refresh(investigation)
        return investigation

    async def get(self, investigation_id: str) -> Investigation:
        """Return an investigation by ID.

        Raises:
            NotFoundError: No such investigation.
        """
        investigation = await self._session.get(Investigation, investigation_id)
        if investigation is None:
            raise NotFoundError(f"investigation {investigation_id} not found")
        return investigation

    async def list(self, *, limit: int = 50) -> Sequence[Investigation]:
        """Return the most recently started investigations first."""
        result = await self._session.scalars(
            select(Investigation)
            .order_by(Investigation.started_at.desc(), Investigation.id.desc())
            .limit(limit)
        )
        return result.all()

    async def set_status(
        self,
        investigation_id: str,
        status: str,
        *,
        finished_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Set the status, and optionally the finish time and error, of an investigation."""
        values: dict[str, Any] = {"status": status}
        if finished_at is not None:
            values["finished_at"] = finished_at
        if error is not None:
            values["error"] = error
        await self._session.execute(
            update(Investigation).where(Investigation.id == investigation_id).values(**values)
        )

    async def add_tokens(self, investigation_id: str, tokens: int) -> None:
        """Add the tokens of completed model calls to the investigation's total."""
        await self._session.execute(
            update(Investigation)
            .where(Investigation.id == investigation_id)
            .values(tokens_used=Investigation.tokens_used + tokens)
        )


@dataclass(frozen=True)
class StepSpec:
    """A plan step to be stored."""

    domain: str
    goal: str
    agent: str
    suggested_tools: Sequence[str] = ()
    expected_evidence: Sequence[str] = ()


class PlanRepository:
    """Plan versions and their steps."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def add_plan(
        self, investigation_id: str, *, source: str, rationale: str, steps: Sequence[StepSpec]
    ) -> Plan:
        """Add the next plan version with its steps, numbered from 1 in the given order."""
        version = await _next_value(
            self._session, Plan.version, Plan.investigation_id == investigation_id
        )
        plan = Plan(
            investigation_id=investigation_id, version=version, source=source, rationale=rationale
        )
        self._session.add(plan)
        await self._session.flush()
        for seq, spec in enumerate(steps, start=1):
            self._session.add(
                Step(
                    plan_id=plan.id,
                    investigation_id=investigation_id,
                    seq=seq,
                    domain=spec.domain,
                    goal=spec.goal,
                    agent=spec.agent,
                    suggested_tools=list(spec.suggested_tools),
                    expected_evidence=list(spec.expected_evidence),
                )
            )
        await self._session.flush()
        return plan

    async def update_step(
        self,
        step_id: int,
        *,
        status: str,
        findings: Sequence[Mapping[str, Any]] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Update the status, and optionally findings and times, of a step."""
        values: dict[str, Any] = {"status": status}
        if findings is not None:
            values["findings"] = [dict(finding) for finding in findings]
        if started_at is not None:
            values["started_at"] = started_at
        if finished_at is not None:
            values["finished_at"] = finished_at
        await self._session.execute(update(Step).where(Step.id == step_id).values(**values))

    async def list_plans(self, investigation_id: str) -> Sequence[Plan]:
        """Return the plan versions of an investigation, oldest first."""
        result = await self._session.scalars(
            select(Plan).where(Plan.investigation_id == investigation_id).order_by(Plan.version)
        )
        return result.all()

    async def list_steps(self, investigation_id: str) -> Sequence[Step]:
        """Return all steps of an investigation, by plan version and position."""
        result = await self._session.scalars(
            select(Step)
            .join(Plan, Plan.id == Step.plan_id)
            .where(Step.investigation_id == investigation_id)
            .order_by(Plan.version, Step.seq)
        )
        return result.all()


class AssessmentRepository:
    """Per-round judgements of whether the evidence is sufficient."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def add(
        self,
        investigation_id: str,
        *,
        round: int,
        sufficient: bool,
        reason: str,
        hypothesis_category: str | None = None,
        missing: Sequence[str] = (),
        rule_override: bool = False,
    ) -> Assessment:
        """Add the assessment of one round."""
        assessment = Assessment(
            investigation_id=investigation_id,
            round=round,
            sufficient=sufficient,
            reason=reason,
            hypothesis_category=hypothesis_category,
            missing=list(missing),
            rule_override=rule_override,
        )
        self._session.add(assessment)
        await self._session.flush()
        return assessment

    async def list(self, investigation_id: str) -> Sequence[Assessment]:
        """Return the assessments of an investigation by round."""
        result = await self._session.scalars(
            select(Assessment)
            .where(Assessment.investigation_id == investigation_id)
            .order_by(Assessment.round)
        )
        return result.all()


class EvidenceRepository:
    """Evidence, keyed by content-derived IDs within an investigation."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def add(
        self,
        investigation_id: str,
        *,
        evidence_id: str,
        source: str,
        collected_at: datetime,
        raw_content: str,
        redacted_content: str,
        tool: str | None = None,
        step_id: int | None = None,
        call_id: int | None = None,
        identifiers: Sequence[str] = (),
        meta: Mapping[str, Any] | None = None,
    ) -> Evidence:
        """Store evidence; identical evidence collected again keeps the first row.

        Returns:
            The stored row, which is the earlier one when the ID already existed.
        """
        await self._session.execute(
            insert(Evidence)
            .values(
                investigation_id=investigation_id,
                id=evidence_id,
                step_id=step_id,
                call_id=call_id,
                source=source,
                tool=tool,
                collected_at=collected_at,
                raw_content=raw_content,
                redacted_content=redacted_content,
                identifiers=list(identifiers),
                meta=dict(meta or {}),
            )
            .on_conflict_do_nothing(index_elements=[Evidence.investigation_id, Evidence.id])
        )
        return await self.get(investigation_id, evidence_id)

    async def get(self, investigation_id: str, evidence_id: str) -> Evidence:
        """Return one piece of evidence.

        Raises:
            NotFoundError: No such evidence in this investigation.
        """
        evidence = await self._session.get(Evidence, (investigation_id, evidence_id))
        if evidence is None:
            raise NotFoundError(f"evidence {evidence_id} not found in {investigation_id}")
        return evidence

    async def list(self, investigation_id: str) -> Sequence[Evidence]:
        """Return the evidence of an investigation ordered by ID."""
        result = await self._session.scalars(
            select(Evidence)
            .where(Evidence.investigation_id == investigation_id)
            .order_by(Evidence.id)
        )
        return result.all()


class CallRecordRepository:
    """Model and tool call records."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def add(
        self,
        *,
        agent: str,
        kind: str,
        name: str,
        input_summary: str,
        status: str,
        started_at: datetime,
        investigation_id: str | None = None,
        session_id: int | None = None,
        caller: str | None = None,
        output_summary: str | None = None,
        gate_result: Mapping[str, Any] | None = None,
        error: str | None = None,
        retries: int = 0,
        latency_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> CallRecord:
        """Store a call record; summaries must already be redacted and are truncated here."""
        record = CallRecord(
            investigation_id=investigation_id,
            session_id=session_id,
            agent=agent,
            kind=kind,
            name=name,
            caller=caller,
            input_summary=truncate_summary(input_summary),
            output_summary=truncate_summary(output_summary),
            gate_result=dict(gate_result) if gate_result is not None else None,
            status=status,
            error=error,
            retries=retries,
            started_at=started_at,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def list_for_investigation(self, investigation_id: str) -> Sequence[CallRecord]:
        """Return the calls of an investigation in the order they started."""
        result = await self._session.scalars(
            select(CallRecord)
            .where(CallRecord.investigation_id == investigation_id)
            .order_by(CallRecord.started_at, CallRecord.id)
        )
        return result.all()


class EventRepository:
    """Persisted progress events of investigations."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def append(
        self, investigation_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> InvestigationEvent:
        """Append an event with the next sequence number of the investigation.

        The investigation row is locked for the rest of the transaction, so concurrent
        appends to the same investigation get distinct, gapless sequence numbers.
        """
        await self._session.execute(
            select(Investigation.id).where(Investigation.id == investigation_id).with_for_update()
        )
        seq = await _next_value(
            self._session,
            InvestigationEvent.seq,
            InvestigationEvent.investigation_id == investigation_id,
        )
        event = InvestigationEvent(
            investigation_id=investigation_id, seq=seq, type=event_type, payload=dict(payload)
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_after(
        self, investigation_id: str, after_seq: int = 0
    ) -> Sequence[InvestigationEvent]:
        """Return the events with a sequence number greater than ``after_seq``, in order."""
        result = await self._session.scalars(
            select(InvestigationEvent)
            .where(
                InvestigationEvent.investigation_id == investigation_id,
                InvestigationEvent.seq > after_seq,
            )
            .order_by(InvestigationEvent.seq)
        )
        return result.all()


class ReportRepository:
    """Report attempts."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def add_attempt(
        self,
        investigation_id: str,
        *,
        status: str,
        body: Mapping[str, Any],
        schema_version: str,
        gate_result: Mapping[str, Any] | None = None,
    ) -> Report:
        """Store the next report attempt of an investigation."""
        attempt = await _next_value(
            self._session, Report.attempt, Report.investigation_id == investigation_id
        )
        report = Report(
            investigation_id=investigation_id,
            attempt=attempt,
            status=status,
            body=dict(body),
            gate_result=dict(gate_result) if gate_result is not None else None,
            schema_version=schema_version,
        )
        self._session.add(report)
        await self._session.flush()
        return report

    async def get_final(self, investigation_id: str) -> Report | None:
        """Return the published or degraded report, or ``None`` when there is none yet."""
        return await self._session.scalar(
            select(Report)
            .where(
                Report.investigation_id == investigation_id,
                Report.status.in_(("published", "degraded")),
            )
            .order_by(Report.attempt.desc())
            .limit(1)
        )

    async def list_attempts(self, investigation_id: str) -> Sequence[Report]:
        """Return every attempt of an investigation, oldest first."""
        result = await self._session.scalars(
            select(Report)
            .where(Report.investigation_id == investigation_id)
            .order_by(Report.attempt)
        )
        return result.all()


@dataclass(frozen=True)
class SuggestionSpec:
    """A remediation suggestion to be stored."""

    action: str
    risk: str
    command: str | None = None
    citations: Sequence[str] = ()


@dataclass(frozen=True)
class SuggestionState:
    """A suggestion with the decision on it, if any."""

    suggestion: Suggestion
    confirmation: Confirmation | None

    @property
    def state(self) -> str:
        """``pending``, ``confirmed`` or ``rejected``, derived from the confirmation."""
        return "pending" if self.confirmation is None else self.confirmation.decision


class SuggestionRepository:
    """Remediation suggestions and the decisions on them."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def add_all(
        self, investigation_id: str, report_id: int, specs: Sequence[SuggestionSpec]
    ) -> list[Suggestion]:
        """Store the suggestions of a published report, numbered from 1 in the given order."""
        suggestions = [
            Suggestion(
                investigation_id=investigation_id,
                id=ids.suggestion_id(seq, spec.action, spec.command, spec.risk, spec.citations),
                report_id=report_id,
                seq=seq,
                action=spec.action,
                command=spec.command,
                risk=spec.risk,
                citations=list(spec.citations),
            )
            for seq, spec in enumerate(specs, start=1)
        ]
        self._session.add_all(suggestions)
        await self._session.flush()
        return suggestions

    async def list_with_decisions(self, investigation_id: str) -> list[SuggestionState]:
        """Return the suggestions of an investigation with their decisions, by position."""
        rows = await self._session.execute(
            select(Suggestion, Confirmation)
            .outerjoin(
                Confirmation,
                (Confirmation.investigation_id == Suggestion.investigation_id)
                & (Confirmation.suggestion_id == Suggestion.id),
            )
            .where(Suggestion.investigation_id == investigation_id)
            .order_by(Suggestion.report_id, Suggestion.seq)
        )
        return [SuggestionState(suggestion=s, confirmation=c) for s, c in rows]


class ConfirmationRepository:
    """Human decisions on suggestions. Append-only: there is no update or delete."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def decide(
        self,
        investigation_id: str,
        suggestion_id: str,
        *,
        decision: str,
        actor: str,
        comment: str | None = None,
    ) -> Confirmation:
        """Record the decision on a suggestion.

        Raises:
            AlreadyDecidedError: The suggestion has already been decided.
        """
        confirmation = Confirmation(
            investigation_id=investigation_id,
            suggestion_id=suggestion_id,
            decision=decision,
            actor=actor,
            comment=comment,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(confirmation)
                await self._session.flush()
        except IntegrityError as exc:
            if "uq_confirmations_investigation_id_suggestion_id" in str(exc.orig):
                raise AlreadyDecidedError(
                    f"suggestion {suggestion_id} of {investigation_id} is already decided"
                ) from exc
            raise
        await self._session.refresh(confirmation, ["decided_at"])
        return confirmation


class FollowupRepository:
    """Follow-up sessions and their messages."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def create_session(self, investigation_id: str, *, created_by: str) -> FollowupSession:
        """Create a follow-up session for an investigation."""
        followup = FollowupSession(investigation_id=investigation_id, created_by=created_by)
        self._session.add(followup)
        await self._session.flush()
        return followup

    async def get_session(self, session_id: int) -> FollowupSession:
        """Return a follow-up session by ID.

        Raises:
            NotFoundError: No such session.
        """
        followup = await self._session.get(FollowupSession, session_id)
        if followup is None:
            raise NotFoundError(f"follow-up session {session_id} not found")
        return followup

    async def add_message(
        self,
        session_id: int,
        *,
        role: str,
        content: str,
        citations: Sequence[str] = (),
        references: Sequence[str] = (),
        unknown: bool = False,
    ) -> FollowupMessage:
        """Append a message with the next sequence number of the session.

        The session row is locked for the rest of the transaction, so concurrent appends
        get distinct sequence numbers.
        """
        await self._session.execute(
            select(FollowupSession.id).where(FollowupSession.id == session_id).with_for_update()
        )
        seq = await _next_value(
            self._session, FollowupMessage.seq, FollowupMessage.session_id == session_id
        )
        message = FollowupMessage(
            session_id=session_id,
            seq=seq,
            role=role,
            content=content,
            citations=list(citations),
            references=list(references),
            unknown=unknown,
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def list_messages(self, session_id: int) -> Sequence[FollowupMessage]:
        """Return the messages of a session in order."""
        result = await self._session.scalars(
            select(FollowupMessage)
            .where(FollowupMessage.session_id == session_id)
            .order_by(FollowupMessage.seq)
        )
        return result.all()


@dataclass(frozen=True)
class ChunkSpec:
    """A knowledge chunk to be stored."""

    heading_path: str
    content: str
    embedding: Sequence[float]


class KnowledgeRepository:
    """Imported runbooks and their chunks."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session."""
        self._session = session

    async def upsert_document(self, *, path: str, title: str, sha256: str) -> KnowledgeDocument:
        """Insert a document, or update the title, hash and import time of the same path."""
        document_id = await self._session.scalar(
            insert(KnowledgeDocument)
            .values(path=path, title=title, sha256=sha256)
            .on_conflict_do_update(
                index_elements=[KnowledgeDocument.path],
                set_={"title": title, "sha256": sha256, "ingested_at": func.now()},
            )
            .returning(KnowledgeDocument.id)
        )
        document = await self._session.get(KnowledgeDocument, document_id, populate_existing=True)
        if document is None:  # pragma: no cover - the row was just written
            raise NotFoundError(f"knowledge document {path} not found")
        return document

    async def replace_chunks(
        self, document: KnowledgeDocument, chunks: Sequence[ChunkSpec]
    ) -> list[KnowledgeChunk]:
        """Replace all chunks of a document, numbered from 0 in the given order."""
        existing = await self._session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
        )
        for chunk in existing.all():
            await self._session.delete(chunk)
        await self._session.flush()
        stored = [
            KnowledgeChunk(
                id=ids.knowledge_id(document.path, spec.heading_path, ordinal),
                document_id=document.id,
                heading_path=spec.heading_path,
                ordinal=ordinal,
                content=spec.content,
                embedding=list(spec.embedding),
            )
            for ordinal, spec in enumerate(chunks)
        ]
        self._session.add_all(stored)
        await self._session.flush()
        return stored
