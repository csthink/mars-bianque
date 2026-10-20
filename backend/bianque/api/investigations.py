"""Investigations: reads, manual triggers, follow-up sessions and decisions.

Reading needs no login: the list, the detail, the calls, the progress events and the
recorded questions. Writing needs a login: manual triggers, opening a follow-up session
and deciding on a suggestion.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.events import TERMINAL_EVENTS, EventNotifier
from bianque.agents.followup import FollowupService, NoReportError
from bianque.agents.service import (
    CaseNotFoundError,
    InvestigationService,
    PackBackendRequiredError,
)
from bianque.api import sse
from bianque.api.auth import CurrentUser
from bianque.api.schemas import (
    AlertView,
    AlreadyDecided,
    AssessmentView,
    CallList,
    CallTotals,
    CallView,
    DecisionRequest,
    EvidenceView,
    FindingView,
    FollowupSessionView,
    GateView,
    InvestigationDetail,
    InvestigationInfo,
    InvestigationList,
    InvestigationSummary,
    PlanView,
    QuestionList,
    RecordedQuestionView,
    RejectedAttempt,
    ReportView,
    StepView,
    SuggestionView,
    TriggerRequest,
    TriggerResult,
)
from bianque.storage.errors import AlreadyDecidedError, NotFoundError
from bianque.storage.models import Alert, CallRecord, Evidence, Investigation, Report
from bianque.storage.repositories import (
    AssessmentRepository,
    CallRecordRepository,
    ConfirmationRepository,
    EventRepository,
    EvidenceRepository,
    InvestigationRepository,
    PlanRepository,
    ReportRepository,
    SuggestionRepository,
    SuggestionState,
)

router = APIRouter(prefix="/api/investigations", tags=["investigations"])

#: Seconds an event stream waits for a wake-up before it reads the table again.
EVENT_POLL_SECONDS = 1.0

#: Seconds without events after which an event stream sends a keep-alive comment.
KEEPALIVE_SECONDS = 15.0


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session:
        yield session


Session = Annotated[AsyncSession, Depends(_session)]


async def _investigation(session: AsyncSession, investigation_id: str) -> Investigation:
    try:
        return await InvestigationRepository(session).get(investigation_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="investigation not found") from None


@router.get("", response_model=InvestigationList)
async def list_investigations(
    session: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> InvestigationList:
    """List the most recently started investigations."""
    rows = await session.execute(
        select(Investigation, Alert)
        .outerjoin(Alert, Alert.id == Investigation.alert_id)
        .order_by(Investigation.started_at.desc(), Investigation.id.desc())
        .limit(limit)
    )
    pairs = rows.all()
    finals = await session.execute(
        select(Report.investigation_id, Report.status).where(
            Report.investigation_id.in_([inv.id for inv, _ in pairs]),
            Report.status.in_(("published", "degraded")),
        )
    )
    report_status = {investigation_id: status for investigation_id, status in finals.all()}
    return InvestigationList(
        investigations=[
            InvestigationSummary(
                id=inv.id,
                status=inv.status,
                trigger=inv.trigger,
                case_id=inv.case_id,
                alert_name=_label(alert, "alertname"),
                namespace=_label(alert, "namespace"),
                pod=_label(alert, "pod"),
                started_at=inv.started_at,
                finished_at=inv.finished_at,
                report_status=_final_status(report_status.get(inv.id)),
            )
            for inv, alert in pairs
        ]
    )


def _final_status(status: str | None) -> Literal["published", "degraded"] | None:
    if status == "published":
        return "published"
    return "degraded" if status == "degraded" else None


def _label(alert: Alert | None, name: str) -> str | None:
    if alert is None:
        return None
    value = alert.labels.get(name)
    return None if value is None else str(value)


@router.get("/{investigation_id}", response_model=InvestigationDetail)
async def get_investigation(investigation_id: str, session: Session) -> InvestigationDetail:
    """Return everything the detail page shows about one investigation."""
    inv = await _investigation(session, investigation_id)
    alert = await session.get(Alert, inv.alert_id) if inv.alert_id is not None else None
    plans_repo = PlanRepository(session)
    plans = await plans_repo.list_plans(inv.id)
    steps = await plans_repo.list_steps(inv.id)
    evidence = await EvidenceRepository(session).list(inv.id)
    attempts = await ReportRepository(session).list_attempts(inv.id)
    final = await ReportRepository(session).get_final(inv.id)
    suggestions = await SuggestionRepository(session).list_with_decisions(inv.id)
    alert_evidence = next((e.id for e in evidence if e.source == "alertmanager"), None)
    return InvestigationDetail(
        investigation=InvestigationInfo(
            id=inv.id,
            status=inv.status,
            trigger=inv.trigger,
            case_id=inv.case_id,
            tool_backend=inv.tool_backend,
            llm_mode=inv.llm_mode,
            llm_model=inv.llm_model,
            token_budget=inv.token_budget,
            tokens_used=inv.tokens_used,
            started_at=inv.started_at,
            finished_at=inv.finished_at,
            error=inv.error,
        ),
        alert=None
        if alert is None
        else AlertView(
            name=_label(alert, "alertname"),
            status=alert.status,
            fingerprint=alert.fingerprint,
            labels=alert.labels,
            annotations=alert.annotations,
            starts_at=alert.starts_at,
            received_at=alert.received_at,
            evidence_id=alert_evidence,
        ),
        plans=[
            PlanView(
                id=plan.id,
                version=plan.version,
                source=plan.source,
                rationale=plan.rationale,
                created_at=plan.created_at,
                steps=[
                    StepView(
                        id=step.id,
                        seq=step.seq,
                        domain=step.domain,
                        goal=step.goal,
                        agent=step.agent,
                        suggested_tools=[str(tool) for tool in step.suggested_tools],
                        expected_evidence=[str(item) for item in step.expected_evidence],
                        status=step.status,
                        findings=[FindingView.model_validate(f) for f in step.findings or []],
                        started_at=step.started_at,
                        finished_at=step.finished_at,
                    )
                    for step in steps
                    if step.plan_id == plan.id
                ],
            )
            for plan in plans
        ],
        assessments=[
            AssessmentView(
                round=a.round,
                sufficient=a.sufficient,
                reason=a.reason,
                hypothesis_category=a.hypothesis_category,
                missing=[str(item) for item in a.missing],
                rule_override=a.rule_override,
                created_at=a.created_at,
            )
            for a in await AssessmentRepository(session).list(inv.id)
        ],
        evidence=[_evidence_view(e) for e in evidence],
        report=None
        if final is None
        else ReportView(
            id=final.id,
            attempt=final.attempt,
            status=_final_status(final.status) or "published",
            body=final.body,
            created_at=final.created_at,
        ),
        rejected_attempts=[
            RejectedAttempt(
                attempt=report.attempt,
                validators=_validators(report.gate_result),
                created_at=report.created_at,
            )
            for report in attempts
            if report.status == "rejected"
        ],
        suggestions=[_suggestion_view(item) for item in suggestions],
    )


def _suggestion_view(item: SuggestionState) -> SuggestionView:
    confirmation = item.confirmation
    return SuggestionView(
        id=item.suggestion.id,
        seq=item.suggestion.seq,
        action=item.suggestion.action,
        command=item.suggestion.command,
        risk=item.suggestion.risk,
        citations=[str(c) for c in item.suggestion.citations],
        state=_state(item.state),
        decided_by=None if confirmation is None else confirmation.actor,
        decided_at=None if confirmation is None else confirmation.decided_at,
        decision_comment=None if confirmation is None else confirmation.comment,
    )


@router.post(
    "/{investigation_id}/suggestions/{suggestion_id}/decision",
    status_code=201,
    response_model=SuggestionView,
    responses={409: {"model": AlreadyDecided}},
)
async def decide(
    investigation_id: str,
    suggestion_id: str,
    body: DecisionRequest,
    actor: CurrentUser,
    session: Session,
) -> SuggestionView | JSONResponse:
    """Confirm or reject a suggestion, once. The decision is only recorded.

    Nothing else happens on a decision: no tool runs and nothing outside the database
    is called. A decided suggestion answers 409; decisions are never changed or
    removed, and the database rejects UPDATE and DELETE on them.
    """
    # The ID is read once: a rollback below expires the loaded investigation row.
    inv_id = (await _investigation(session, investigation_id)).id
    suggestions = SuggestionRepository(session)
    item = await suggestions.get_with_decision(inv_id, suggestion_id)
    if item is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if item.confirmation is None:
        try:
            await ConfirmationRepository(session).decide(
                inv_id, suggestion_id, decision=body.decision, actor=actor, comment=body.comment
            )
        except AlreadyDecidedError:
            # Another request decided between the lookup and the insert.
            await session.rollback()
            item = await suggestions.get_with_decision(inv_id, suggestion_id)
        else:
            await session.commit()
            decided = await suggestions.get_with_decision(inv_id, suggestion_id)
            assert decided is not None
            return _suggestion_view(decided)
    assert item is not None and item.confirmation is not None
    return JSONResponse(
        status_code=409,
        content=AlreadyDecided(state=_decided(item.confirmation.decision)).model_dump(),
    )


def _evidence_view(evidence: Evidence) -> EvidenceView:
    arguments = evidence.meta.get("arguments")
    return EvidenceView(
        id=evidence.id,
        step_id=evidence.step_id,
        call_id=evidence.call_id,
        source=evidence.source,
        tool=evidence.tool,
        collected_at=evidence.collected_at,
        redacted_content=evidence.redacted_content,
        arguments=arguments if isinstance(arguments, dict) else {},
    )


def _state(state: str) -> Literal["pending", "confirmed", "rejected"]:
    if state == "confirmed":
        return "confirmed"
    return "rejected" if state == "rejected" else "pending"


def _decided(decision: str) -> Literal["confirmed", "rejected"]:
    return "confirmed" if decision == "confirmed" else "rejected"


def _validators(gate_result: dict[str, Any] | None) -> list[str]:
    """Return the validator names of a stored gate result, and nothing else of it."""
    if not gate_result:
        return []
    violations = gate_result.get("violations") or []
    return sorted({str(v.get("validator")) for v in violations if isinstance(v, dict)})


@router.get("/{investigation_id}/calls", response_model=CallList)
async def list_calls(investigation_id: str, session: Session) -> CallList:
    """Return the call records of one investigation with their totals."""
    inv = await _investigation(session, investigation_id)
    records = await CallRecordRepository(session).list_for_investigation(inv.id)
    produced = await session.execute(
        select(Evidence.call_id, Evidence.id)
        .where(Evidence.investigation_id == inv.id, Evidence.call_id.is_not(None))
        .order_by(Evidence.id)
    )
    evidence_by_call: dict[int, list[str]] = {}
    for call_id, evidence_id in produced.all():
        if call_id is not None:
            evidence_by_call.setdefault(call_id, []).append(evidence_id)
    calls = [_call_view(record, evidence_by_call.get(record.id, [])) for record in records]
    end = inv.finished_at or max(
        (r.started_at + timedelta(milliseconds=r.latency_ms or 0) for r in records),
        default=inv.started_at,
    )
    return CallList(
        calls=calls,
        totals=CallTotals(
            calls=len(calls),
            model_calls=sum(1 for c in calls if c.kind == "model"),
            tool_calls=sum(1 for c in calls if c.kind == "tool"),
            knowledge_calls=sum(1 for c in calls if c.kind == "knowledge"),
            tokens_in=sum(c.tokens_in or 0 for c in calls),
            tokens_out=sum(c.tokens_out or 0 for c in calls),
            latency_ms=sum(c.latency_ms or 0 for c in calls),
            elapsed_ms=max(0, int((end - inv.started_at).total_seconds() * 1000)),
        ),
    )


def _call_view(record: CallRecord, evidence_ids: list[str]) -> CallView:
    kind: Literal["model", "tool", "knowledge"] = (
        "tool" if record.kind == "tool" else "knowledge" if record.kind == "knowledge" else "model"
    )
    # Only a model call's output is model text, which the API never returns.
    shown = kind != "model"
    return CallView(
        id=record.id,
        agent=record.agent,
        kind=kind,
        name=record.name,
        caller=record.caller,
        started_at=record.started_at,
        latency_ms=record.latency_ms,
        status=record.status,
        retries=record.retries,
        input_summary=record.input_summary if shown else None,
        output_summary=record.output_summary if shown else None,
        error=record.error if shown else None,
        evidence_ids=evidence_ids,
        tokens_in=record.tokens_in,
        tokens_out=record.tokens_out,
        gate=None
        if shown or record.gate_result is None
        else GateView(
            passed=bool(record.gate_result.get("passed")),
            validators=_validators(record.gate_result),
        ),
    )


@router.post("", status_code=202, response_model=TriggerResult)
async def trigger(body: TriggerRequest, user: CurrentUser, request: Request) -> TriggerResult:
    """Start an investigation by hand: replay a case, or investigate a stored alert again.

    It is not deduplicated: a person asked for a new investigation.
    """
    service: InvestigationService = request.app.state.investigations
    if (body.case_id is None) == (body.alert_id is None):
        raise HTTPException(status_code=422, detail="give either case_id or alert_id")
    try:
        if body.case_id is not None:
            investigation_id = await service.replay_case(body.case_id)
        else:
            assert body.alert_id is not None
            investigation_id = await service.reinvestigate(body.alert_id)
    except PackBackendRequiredError:
        raise HTTPException(status_code=409, detail="cases replay with the pack backend") from None
    except (CaseNotFoundError, NotFoundError):
        raise HTTPException(status_code=404, detail="case or alert not found") from None
    return TriggerResult(investigation_id=investigation_id)


def _resume_point(after: int, last_event_id: str | None) -> int:
    """The larger of ``after`` and a numeric ``Last-Event-ID``; malformed IDs count as 0."""
    try:
        header = int(last_event_id) if last_event_id is not None else 0
    except ValueError:
        header = 0
    return max(after, header, 0)


@router.get(
    "/{investigation_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def events(
    investigation_id: str,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream the progress events of an investigation, resuming after an event number.

    The stream starts after ``Last-Event-ID`` (sent by ``EventSource`` when it reconnects)
    or after ``after`` (sent by a page that reloads), whichever is larger, and ends after
    ``investigation_finished`` or ``investigation_interrupted``.

    The endpoint takes no request-scoped database session: one would stay open, idle in
    a transaction, for as long as the stream lasts, and an open transaction blocks
    statements that wait for all of them, such as the ``CREATE INDEX CONCURRENTLY`` of
    the checkpoint tables' setup. Each read of the stream uses a session of its own.
    """
    async with request.app.state.sessions() as session:
        inv_id = (await _investigation(session, investigation_id)).id
    return sse.stream(
        _event_frames(
            request.app.state.sessions,
            request.app.state.notifier,
            inv_id,
            _resume_point(after, last_event_id),
        )
    )


async def _event_frames(
    sessions: async_sessionmaker[AsyncSession],
    notifier: EventNotifier,
    investigation_id: str,
    after: int,
) -> AsyncIterator[str]:
    """Read the events after ``after`` from the table, waiting for new ones in between.

    The table is the only source of events; a wake-up only makes the next read sooner.
    The read after each wait also finds events another process wrote.
    """
    last = after
    quiet_since = time.monotonic()
    while True:
        async with sessions() as session:
            rows = await EventRepository(session).list_after(investigation_id, last)
        for row in rows:
            last = row.seq
            data = {
                "seq": row.seq,
                "type": row.type,
                "created_at": row.created_at.isoformat(),
                **row.payload,
            }
            yield sse.frame(row.type, data, event_id=row.seq)
            if row.type in TERMINAL_EVENTS:
                return
        now = time.monotonic()
        if rows:
            quiet_since = now
        elif now - quiet_since >= KEEPALIVE_SECONDS:
            yield sse.comment("keepalive")
            quiet_since = now
        await notifier.wait(investigation_id, EVENT_POLL_SECONDS)


@router.post("/{investigation_id}/sessions", status_code=201, response_model=FollowupSessionView)
async def create_session(
    investigation_id: str, user: CurrentUser, request: Request
) -> FollowupSessionView:
    """Open a follow-up session on an investigation that has a report."""
    followups: FollowupService = request.app.state.followups
    try:
        created = await followups.create_session(investigation_id, created_by=user)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="investigation not found") from None
    except NoReportError:
        raise HTTPException(status_code=409, detail="no_report") from None
    return FollowupSessionView(
        id=created.id,
        investigation_id=created.investigation_id,
        created_by=created.created_by,
        created_at=created.created_at,
    )


@router.get("/{investigation_id}/questions", response_model=QuestionList)
async def questions(investigation_id: str, request: Request) -> QuestionList:
    """The questions of the investigation's case, from its ``questions.yaml``.

    In ``recorded`` mode only these can be asked, the n-th turn of a session asking the
    n-th question, since a recorded answer depends on the turns before it.
    """
    followups: FollowupService = request.app.state.followups
    try:
        recorded = await followups.questions(investigation_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="investigation not found") from None
    return QuestionList(
        recorded_only=followups.recorded_only,
        questions=[RecordedQuestionView(id=q.id, question=q.question) for q in recorded],
    )
