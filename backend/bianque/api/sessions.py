"""Follow-up sessions: read one (no login), and ask a question (login).

The answer to a question is a stream of server-sent events. Until the
answer has passed the evidence gate, the stream carries ``progress`` events only: a
stage and the elapsed time. Then the paragraphs follow as ``content`` events and the
stream ends with ``done``; or it ends with ``failed``, a reason and validator names. No
model text reaches the client before it has passed the gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from bianque.agents.followup import (
    Content,
    Done,
    Failed,
    FollowupEvent,
    FollowupService,
    Progress,
    QuestionNotRecordedError,
    SessionBusyError,
    session_messages,
)
from bianque.api import sse
from bianque.api.auth import CurrentUser
from bianque.api.schemas import (
    AnswerSegmentView,
    FollowupMessageView,
    FollowupSessionDetail,
    FollowupSessionView,
    NotRecorded,
    QuestionRequest,
)
from bianque.storage.errors import NotFoundError

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/{session_id}", response_model=FollowupSessionDetail)
async def get_session(session_id: int, request: Request) -> FollowupSessionDetail:
    """A follow-up session with its questions and the answers that passed the gate."""
    try:
        followup, messages = await session_messages(request.app.state.sessions, session_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="session not found") from None
    return FollowupSessionDetail(
        session=FollowupSessionView(
            id=followup.id,
            investigation_id=followup.investigation_id,
            created_by=followup.created_by,
            created_at=followup.created_at,
        ),
        messages=[
            FollowupMessageView(
                id=message.id,
                seq=message.seq,
                role=_role(message.role),
                content=message.content,
                segments=[AnswerSegmentView.model_validate(item) for item in message.segments],
                citations=[str(c) for c in message.citations],
                references=[str(r) for r in message.references],
                unknown=message.unknown,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )


def _role(role: str) -> Literal["user", "assistant"]:
    return "assistant" if role == "assistant" else "user"


@router.post(
    "/{session_id}/messages",
    response_class=StreamingResponse,
    response_model=None,
    responses={
        200: {"content": {"text/event-stream": {}}},
        422: {"model": NotRecorded},
    },
)
async def ask(
    session_id: int, body: QuestionRequest, user: CurrentUser, request: Request
) -> StreamingResponse | JSONResponse:
    """Ask a question; the answer is streamed once it has passed the evidence gate."""
    followups: FollowupService = request.app.state.followups
    try:
        events = await followups.ask(session_id, body.question)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="session not found") from None
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="session_busy") from None
    except QuestionNotRecordedError as exc:
        return JSONResponse(
            status_code=422, content=NotRecorded(expected=exc.expected).model_dump()
        )
    return sse.stream(_frames(events))


async def _frames(events: AsyncIterator[FollowupEvent]) -> AsyncIterator[str]:
    async for event in events:
        yield _frame(event)


def _frame(event: FollowupEvent) -> str:
    """Encode one event; each event type has its own fixed fields."""
    if isinstance(event, Progress):
        return sse.frame("progress", asdict(event))
    if isinstance(event, Content):
        return sse.frame(
            "content",
            {
                "segment_index": event.segment_index,
                "text": event.text,
                "citations": list(event.citations),
            },
        )
    if isinstance(event, Done):
        return sse.frame("done", {"message_id": event.message_id, "validated": True})
    assert isinstance(event, Failed)
    return sse.frame("failed", {"reason": event.reason, "violations": list(event.violations)})
