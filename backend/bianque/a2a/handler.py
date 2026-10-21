"""The A2A operations, as the SDK's request handler.

``SendMessage`` and ``SendStreamingMessage`` start a task (``bianque.a2a.intake``): an
alert goes the way of a webhook alert, through ``InvestigationService.deliver`` with its
fingerprint deduplication, and a task for an alert under investigation follows that
investigation; a case is replayed as a new investigation. Both carry the trigger
``a2a``, and nothing about the investigation differs from one started otherwise: the
same prompts, tools and graph, so the same model requests.

Progress comes from the investigation's events, read with ``follow_events`` as the
investigation's own event stream reads them. A stream starts with the task; for a new
investigation the task is ``SUBMITTED`` and every event of the investigation follows as
a status update; for a task that joined an investigation, or one subscribed to again,
the task reflects every event so far and the later ones follow. At the end come the
artifact, when there is a report, and the final status.

Not offered: push notifications, cancelling (an investigation runs to its end), further
messages to a task (Bianque does not converse; a task ends with its report), and the
extended Agent Card. A failure that is not an A2A error is logged and answered as an
internal error that tells nothing of it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from dataclasses import dataclass

from a2a.server.context import ServerCallContext
from a2a.server.events import Event
from a2a.server.request_handlers import RequestHandler
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    InternalError,
    InvalidParamsError,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    PushNotificationNotSupportedError,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskPushNotificationConfig,
    TaskStatusUpdateEvent,
    UnsupportedOperationError,
)
from a2a.utils.errors import A2AError
from a2a.utils.proto_utils import validate_proto_required_fields
from a2a.utils.task import validate_history_length
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.a2a.clients import caller
from bianque.a2a.intake import AlertRequest, CaseRequest, data_contents, read_request
from bianque.a2a.tasks import TERMINAL_STATES, BianqueTaskStore, is_terminal
from bianque.agents.events import EventNotifier
from bianque.agents.records import follow_events
from bianque.agents.service import InvestigationService
from bianque.config import Settings
from bianque.storage import ids
from bianque.storage.models import A2ATask
from bianque.storage.repositories import A2ATaskRepository

logger = logging.getLogger(__name__)

#: Seconds a stream waits for a wake-up before it reads the events again.
EVENT_POLL_SECONDS = 1.0

#: The longest context ID a client may give.
CONTEXT_ID_LIMIT = 256

#: The trigger of every investigation an A2A task starts.
TRIGGER = "a2a"


@dataclass(frozen=True)
class _Started:
    """A task just created.

    Attributes:
        row: Its row.
        joined: It follows an investigation that was already under way.
    """

    row: A2ATask
    joined: bool


class BianqueRequestHandler(RequestHandler):
    """Serves the A2A operations over investigations."""

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        notifier: EventNotifier,
        service: InvestigationService,
        store: BianqueTaskStore,
    ) -> None:
        """Start investigations with ``service``; read tasks through ``store``.

        Args:
            settings: Process settings: the tool backend and the cases directory.
            sessions: Database sessions.
            notifier: Wakes the readers of an investigation's events.
            service: Starts and runs investigations.
            store: The client-scoped task store.
        """
        self._settings = settings
        self._sessions = sessions
        self._notifier = notifier
        self._service = service
        self._store = store

    # Reads --------------------------------------------------------------------------

    async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
        """Return a task of the caller as it is now."""

        async def run() -> Task:
            validate_proto_required_fields(params)
            validate_history_length(params)
            task = await self._store.get(params.id, context)
            if task is None:
                raise TaskNotFoundError
            return task

        return await _guarded("GetTask", run())

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        """List the caller's tasks."""

        async def run() -> ListTasksResponse:
            validate_proto_required_fields(params)
            validate_history_length(params)
            return await self._store.list(params, context)

        return await _guarded("ListTasks", run())

    # Messages -----------------------------------------------------------------------

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | Message:
        """Start a task; return it when it ends, or at once with ``returnImmediately``."""

        async def run() -> Task:
            started = await self._start(params, context)
            row = started.row
            if row.investigation_id is not None and not params.configuration.return_immediately:
                async for _ in follow_events(
                    self._sessions,
                    self._notifier,
                    row.investigation_id,
                    0,
                    poll_seconds=EVENT_POLL_SECONDS,
                ):
                    pass
            return await self._store.reader.task(row)

        return await _guarded("SendMessage", run())

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        """Start a task and stream it until it ends."""

        async def run() -> AsyncIterator[Event]:
            started = await self._start(params, context)
            reader = self._store.reader
            if started.joined or started.row.investigation_id is None:
                task, after = await reader.snapshot(started.row)
            else:
                task, after = reader.submitted(started.row), 0
            yield task
            if task.status.state not in TERMINAL_STATES:
                async for event in self._follow(started.row, after):
                    yield event

        async for event in _guarded_stream("SendStreamingMessage", run()):
            yield event

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        """Stream a task of the caller that has not ended, from its current state."""

        async def run() -> AsyncIterator[Event]:
            validate_proto_required_fields(params)
            row = await self._store.owned(params.id, context)
            if row is None:
                raise TaskNotFoundError
            task, after = await self._store.reader.snapshot(row)
            if task.status.state in TERMINAL_STATES:
                raise UnsupportedOperationError(message="the task has ended; read it with GetTask")
            yield task
            async for event in self._follow(row, after):
                yield event

        async for event in _guarded_stream("SubscribeToTask", run()):
            yield event

    # Not offered --------------------------------------------------------------------

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        """Refuse: an investigation runs to its end."""

        async def run() -> Task:
            validate_proto_required_fields(params)
            if await self._store.owned(params.id, context) is None:
                raise TaskNotFoundError
            raise TaskNotCancelableError(message="an investigation runs to its end")

        return await _guarded("CancelTask", run())

    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        """Refuse: Bianque sends no push notifications."""
        raise PushNotificationNotSupportedError

    async def on_get_task_push_notification_config(
        self, params: GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        """Refuse: Bianque sends no push notifications."""
        raise PushNotificationNotSupportedError

    async def on_list_task_push_notification_configs(
        self, params: ListTaskPushNotificationConfigsRequest, context: ServerCallContext
    ) -> ListTaskPushNotificationConfigsResponse:
        """Refuse: Bianque sends no push notifications."""
        raise PushNotificationNotSupportedError

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        """Refuse: Bianque sends no push notifications."""
        raise PushNotificationNotSupportedError

    async def on_get_extended_agent_card(
        self, params: GetExtendedAgentCardRequest, context: ServerCallContext
    ) -> AgentCard:
        """Refuse: the public Agent Card is the only one."""
        raise UnsupportedOperationError(message="there is no extended Agent Card")

    # Starting and following ----------------------------------------------------------

    async def _start(self, params: SendMessageRequest, context: ServerCallContext) -> _Started:
        """Create the task of a message: following an investigation, or rejected.

        Raises:
            TaskNotFoundError: The message names a task the caller does not have.
            UnsupportedOperationError: The message names a task of the caller.
            ContentTypeNotSupportedError: A part of the message is not a data part.
            InvalidParamsError: The context ID is too long.
        """
        validate_proto_required_fields(params)
        message = params.message
        if message.task_id:
            if await self._store.owned(message.task_id, context) is None:
                raise TaskNotFoundError
            raise UnsupportedOperationError(
                message="a task takes no further messages; send a new message without a task ID"
            )
        contents = data_contents(message.parts)
        if len(message.context_id) > CONTEXT_ID_LIMIT:
            raise InvalidParamsError(
                message=f"a context ID has at most {CONTEXT_ID_LIMIT} characters"
            )
        client = caller(context)
        request = read_request(contents, self._settings, client.namespaces)
        investigation_id: str | None = None
        rejected_reason: str | None = None
        joined = False
        if isinstance(request, CaseRequest):
            investigation_id = await self._service.replay_case(request.case_id, trigger=TRIGGER)
        elif isinstance(request, AlertRequest):
            [delivered] = await self._service.deliver([request.alert], trigger=TRIGGER)
            joined = delivered.deduplicated
            investigation_id = delivered.investigation_id or delivered.open_investigation_id
            assert investigation_id is not None, "a firing alert is investigated or joined"
        else:
            rejected_reason = request.reason
        async with self._sessions() as session, session.begin():
            row = await A2ATaskRepository(session).create(
                context_id=message.context_id or ids.new_a2a_context_id(),
                caller=client.name,
                investigation_id=investigation_id,
                rejected_reason=rejected_reason,
            )
        logger.info(
            "A2A task %s of %s: %s",
            row.id,
            client.name,
            f"rejected ({rejected_reason})"
            if rejected_reason
            else f"{'joined' if joined else 'started'} {investigation_id}",
        )
        return _Started(row, joined)

    async def _follow(
        self, row: A2ATask, after: int
    ) -> AsyncIterator[TaskStatusUpdateEvent | TaskArtifactUpdateEvent]:
        """The updates of a task for the events after ``after``, until the investigation ends."""
        assert row.investigation_id is not None
        reader = self._store.reader
        async for event in follow_events(
            self._sessions,
            self._notifier,
            row.investigation_id,
            after,
            poll_seconds=EVENT_POLL_SECONDS,
        ):
            if event is None:
                continue
            if not is_terminal(event):
                yield reader.update(row, event)
                continue
            task = await reader.task(row)
            for artifact in task.artifacts:
                yield TaskArtifactUpdateEvent(
                    task_id=row.id, context_id=row.context_id, artifact=artifact, last_chunk=True
                )
            yield TaskStatusUpdateEvent(
                task_id=row.id, context_id=row.context_id, status=task.status
            )


async def _guarded[T](operation: str, work: Awaitable[T]) -> T:
    """Await ``work``; a failure that is not an A2A error becomes an opaque internal error."""
    try:
        return await work
    except A2AError:
        raise
    except Exception:
        logger.exception("A2A %s failed", operation)
        raise InternalError from None


async def _guarded_stream(operation: str, events: AsyncIterator[Event]) -> AsyncIterator[Event]:
    """Pass ``events`` on; a failure that is not an A2A error becomes an internal error."""
    try:
        async for event in events:
            yield event
    except A2AError:
        raise
    except Exception:
        logger.exception("A2A %s failed", operation)
        raise InternalError from None
