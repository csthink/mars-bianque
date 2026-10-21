"""A2A tasks as clients see them, derived from investigations whenever they are read.

A task's state is never stored. ``a2a_tasks`` only links a task to its client, its
context and the investigation it follows, or records why it was rejected; the state
comes from the investigation:

- rejected: ``REJECTED``, the reason code and a sentence;
- ``pending``: ``SUBMITTED``;
- ``running``: ``WORKING``, described by the latest progress event;
- ``reported`` or ``degraded``: ``COMPLETED``, with the report as the artifact ``report``;
- ``failed`` or ``interrupted``: ``FAILED`` and why.

A status message is built by code from an event's type and payload, which hold IDs,
statuses, counters and validator names only (``bianque.agents.events``), so no model
text reaches a status. The artifact carries the report that passed the evidence gate,
the same body ``GET /api/investigations/{id}`` returns, and a text summary built from it.

``BianqueTaskStore`` is the task store of the A2A SDK. It reads a task only for the
client that started it: another client's task reads as missing, exactly like one that
does not exist, so a client never learns that it exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import (
    Artifact,
    InvalidParamsError,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    UnsupportedOperationError,
)
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.task import (
    ListTasksCursor,
    decode_list_tasks_cursor,
    encode_list_tasks_cursor,
    validate_page_size,
)
from google.protobuf import struct_pb2
from google.protobuf.json_format import ParseDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.a2a.clients import caller
from bianque.a2a.intake import REJECTIONS
from bianque.agents.events import TERMINAL_EVENTS
from bianque.storage.models import A2ATask, Investigation, InvestigationEvent
from bianque.storage.repositories import (
    A2ATaskRepository,
    EventRepository,
    InvestigationRepository,
    ReportRepository,
    SuggestionRepository,
)

#: States after which a task changes no more.
TERMINAL_STATES = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)

#: The name and ID of the report artifact.
REPORT_ARTIFACT = "report"


def _data_part(value: Any) -> Part:
    return Part(data=ParseDict(value, struct_pb2.Value()), media_type="application/json")


def _text_part(text: str) -> Part:
    return Part(text=text, media_type="text/plain")


def _message(task: A2ATask, key: str, text: str, data: Mapping[str, Any]) -> Message:
    return Message(
        message_id=f"{task.id}:{key}",
        context_id=task.context_id,
        task_id=task.id,
        role=Role.ROLE_AGENT,
        parts=[_text_part(text), _data_part(dict(data))],
    )


def _describe(event_type: str, payload: Mapping[str, Any]) -> str:
    """The text of a progress event: its type, IDs and enumerations, never model text."""
    match event_type:
        case "investigation_started":
            return f"investigation started ({payload['trigger']})"
        case "plan_created":
            return f"plan version {payload['version']} ({payload['source']})"
        case "step_started":
            return f"step {payload['step_id']} started ({payload['domain']})"
        case "step_finished":
            return f"step {payload['step_id']} {payload['status']} ({payload['domain']})"
        case "evidence_added":
            origin = ", ".join(str(v) for v in (payload["source"], payload.get("tool")) if v)
            return f"evidence {payload['evidence_id']} added ({origin})"
        case "assessment_recorded":
            verdict = "sufficient" if payload["sufficient"] else "insufficient"
            return f"round {payload['round']} assessment: {verdict}"
        case "report_rejected":
            return (
                f"report attempt {payload['attempt']} rejected by "
                f"{', '.join(payload['violations']) or 'schema'}"
            )
        case "report_published":
            return "report published"
        case "report_degraded":
            return "degraded report published"
        case "investigation_finished":
            return f"investigation finished: {payload['status']}"
        case "investigation_interrupted":
            return "investigation interrupted"
    return event_type


def event_message(task: A2ATask, event: InvestigationEvent) -> Message:
    """The status message of a progress event: its description and its payload as data."""
    data = {"event": event.type, "seq": event.seq, **event.payload}
    return _message(task, str(event.seq), _describe(event.type, event.payload), data)


def detail_url(public_url: str, investigation_id: str) -> str:
    """The investigation's page: the pages share the origin of the A2A endpoint."""
    return urljoin(public_url, f"/investigations/{investigation_id}")


@dataclass(frozen=True)
class FinalReport:
    """The published or degraded report of an investigation, and its suggestions.

    Attributes:
        status: ``published`` or ``degraded``.
        body: The report as stored; it passed the evidence gate.
        suggestions: ``{id, action, command, risk, state}`` of each suggestion, where
            ``state`` is ``pending``, ``confirmed`` or ``rejected``.
    """

    status: str
    body: dict[str, Any]
    suggestions: list[dict[str, Any]]


def report_text(
    report: Mapping[str, Any], status: str, suggestions: Sequence[Mapping[str, Any]], url: str
) -> str:
    """A plain text summary of a report, built from its fields without any model call."""
    cause = report.get("root_cause") or {}
    traits = [str(cause.get("status", "unknown"))]
    if cause.get("category"):
        traits.append(f"category {cause['category']}")
    if cause.get("confidence"):
        traits.append(f"confidence {cause['confidence']}")
    lines = [
        f"Report: {status}",
        f"Summary: {report.get('summary', '')}",
        f"Root cause ({', '.join(traits)}): {cause.get('statement', '')}",
    ]
    if suggestions:
        lines.append("Suggestions:")
        for number, item in enumerate(suggestions, start=1):
            command = f" `{item['command']}`" if item.get("command") else ""
            marks = f"{item['risk']}, {item['state']}"
            lines.append(f"{number}. [{marks}] {item['action']}{command} ({item['id']})")
    else:
        lines.append("Suggestions: none")
    lines.append(f"Details and confirmation: {url}")
    return "\n".join(lines)


def report_artifact(investigation_id: str, report: FinalReport, public_url: str) -> Artifact:
    """The artifact ``report``: the report as JSON, and its text summary."""
    url = detail_url(public_url, investigation_id)
    data = {
        "investigation_id": investigation_id,
        "report_status": report.status,
        "report": report.body,
        "suggestions": report.suggestions,
        "detail_url": url,
    }
    return Artifact(
        artifact_id=REPORT_ARTIFACT,
        name=REPORT_ARTIFACT,
        description="The investigation report: every conclusion cites its evidence.",
        parts=[
            _data_part(data),
            _text_part(report_text(report.body, report.status, report.suggestions, url)),
        ],
    )


class TaskReader:
    """Derives tasks from their rows and the investigations they follow."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], public_url: str) -> None:
        """Read with ``sessions``; detail page addresses share the origin of ``public_url``."""
        self._sessions = sessions
        self._public_url = public_url

    async def task(self, row: A2ATask, *, artifacts: bool = True) -> Task:
        """The task as it is now."""
        task, _ = await self.snapshot(row, artifacts=artifacts)
        return task

    async def snapshot(self, row: A2ATask, *, artifacts: bool = True) -> tuple[Task, int]:
        """The task as it is now, and the sequence number of the latest event it reflects."""
        if row.investigation_id is None:
            return self._rejected(row), 0
        async with self._sessions() as session:
            investigation = await InvestigationRepository(session).get(row.investigation_id)
            latest = await EventRepository(session).latest(row.investigation_id)
            report = (
                await _report(session, row.investigation_id)
                if artifacts and investigation.status in ("reported", "degraded")
                else None
            )
        status = self._status(row, investigation, latest)
        return self._task(row, status, report), latest.seq if latest is not None else 0

    def submitted(self, row: A2ATask) -> Task:
        """The task as it was when it was created for a new investigation."""
        status = TaskStatus(
            state=TaskState.TASK_STATE_SUBMITTED,
            message=_message(row, "submitted", "investigation queued", {"status": "pending"}),
            timestamp=row.created_at,
        )
        return self._task(row, status, None)

    def update(self, row: A2ATask, event: InvestigationEvent) -> TaskStatusUpdateEvent:
        """The status update for a progress event that does not end the investigation."""
        return TaskStatusUpdateEvent(
            task_id=row.id,
            context_id=row.context_id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_WORKING,
                message=event_message(row, event),
                timestamp=event.created_at,
            ),
        )

    def _rejected(self, row: A2ATask) -> Task:
        reason = row.rejected_reason or "malformed"
        status = TaskStatus(
            state=TaskState.TASK_STATE_REJECTED,
            message=_message(
                row, "rejected", f"{reason}: {REJECTIONS[reason]}", {"reason": reason}
            ),
            timestamp=row.created_at,
        )
        return self._task(row, status, None)

    def _status(
        self, row: A2ATask, investigation: Investigation, latest: InvestigationEvent | None
    ) -> TaskStatus:
        at: datetime = latest.created_at if latest is not None else row.created_at
        match investigation.status:
            case "pending":
                state = TaskState.TASK_STATE_SUBMITTED
                message = _message(row, "submitted", "investigation queued", {"status": "pending"})
            case "running":
                state = TaskState.TASK_STATE_WORKING
                message = (
                    event_message(row, latest)
                    if latest is not None
                    else _message(row, "running", "investigation running", {"status": "running"})
                )
            case "reported" | "degraded":
                state = TaskState.TASK_STATE_COMPLETED
                message = _message(
                    row,
                    "completed",
                    f"investigation finished: {investigation.status}",
                    {"status": investigation.status},
                )
            case "interrupted":
                state = TaskState.TASK_STATE_FAILED
                message = _message(
                    row,
                    "failed",
                    "investigation interrupted by a restart",
                    {"reason": "investigation_interrupted"},
                )
            case _:
                state = TaskState.TASK_STATE_FAILED
                message = _message(
                    row,
                    "failed",
                    f"investigation failed: {investigation.error or 'unknown error'}",
                    {"reason": "investigation_failed", "error": investigation.error},
                )
        return TaskStatus(state=state, message=message, timestamp=at)

    def _task(self, row: A2ATask, status: TaskStatus, report: FinalReport | None) -> Task:
        task = Task(id=row.id, context_id=row.context_id, status=status)
        if row.investigation_id is not None:
            task.metadata.update({"investigation_id": row.investigation_id})
            if report is not None:
                task.artifacts.append(
                    report_artifact(row.investigation_id, report, self._public_url)
                )
        return task


async def _report(session: AsyncSession, investigation_id: str) -> FinalReport | None:
    final = await ReportRepository(session).get_final(investigation_id)
    if final is None:
        return None
    suggestions = [
        {
            "id": item.suggestion.id,
            "action": item.suggestion.action,
            "command": item.suggestion.command,
            "risk": item.suggestion.risk,
            "state": item.state,
        }
        for item in await SuggestionRepository(session).list_with_decisions(investigation_id)
    ]
    return FinalReport(final.status, dict(final.body), suggestions)


def is_terminal(event: InvestigationEvent) -> bool:
    """Whether the investigation has no events after this one."""
    return event.type in TERMINAL_EVENTS


class BianqueTaskStore(TaskStore):
    """The SDK's task store over ``a2a_tasks`` and the investigations; read only.

    Every read is scoped to the calling client. Nothing writes a task through the store:
    a task's state is derived, never saved.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], reader: TaskReader) -> None:
        """Read rows with ``sessions`` and derive tasks with ``reader``."""
        self._sessions = sessions
        self.reader = reader

    async def owned(self, task_id: str, context: ServerCallContext) -> A2ATask | None:
        """Return the row of a task of the calling client, or ``None``."""
        async with self._sessions() as session:
            return await A2ATaskRepository(session).get_owned(task_id, caller(context).name)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        """Return a task of the calling client; another client's task reads as missing."""
        row = await self.owned(task_id, context)
        return None if row is None else await self.reader.task(row)

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        """List the tasks of the calling client, newest first.

        The state filters apply to derived states, so tasks are derived before they are
        filtered and paged; one client's tasks are few.
        """
        page_size = (
            params.page_size if params.HasField("page_size") else DEFAULT_LIST_TASKS_PAGE_SIZE
        )
        validate_page_size(page_size)
        async with self._sessions() as session:
            rows = await A2ATaskRepository(session).list_owned(
                caller(context).name, context_id=params.context_id or None
            )
        listed: list[tuple[A2ATask, Task]] = []
        for row in rows:
            task = await self.reader.task(row, artifacts=params.include_artifacts)
            if params.status and task.status.state != params.status:
                continue
            if params.HasField("status_timestamp_after") and (
                task.status.timestamp.ToNanoseconds()
                <= params.status_timestamp_after.ToNanoseconds()
            ):
                continue
            listed.append((row, task))
        start = 0
        if params.page_token:
            cursor = decode_list_tasks_cursor(params.page_token)
            if cursor is None:
                raise InvalidParamsError(message="invalid page token")
            start = next(
                (i for i, (row, _) in enumerate(listed) if _position(row) < cursor.sort_key()),
                len(listed),
            )
        page = listed[start : start + page_size]
        more = start + page_size < len(listed)
        token = encode_list_tasks_cursor(_cursor(page[-1][0])) if more and page else ""
        return ListTasksResponse(
            tasks=[task for _, task in page],
            next_page_token=token,
            page_size=page_size,
            total_size=len(listed),
        )

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Refuse: a task's state is derived from its investigation, never saved."""
        raise UnsupportedOperationError(message="tasks are derived from investigations")

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        """Refuse: tasks are kept as long as their investigations."""
        raise UnsupportedOperationError(message="tasks are not deleted")


def _cursor(row: A2ATask) -> ListTasksCursor:
    stamp = row.created_at
    nanoseconds = int(stamp.timestamp()) * 1_000_000_000 + stamp.microsecond * 1000
    return ListTasksCursor(timestamp_ns=nanoseconds, task_id=row.id)


def _position(row: A2ATask) -> tuple[bool, int, str]:
    return _cursor(row).sort_key()
