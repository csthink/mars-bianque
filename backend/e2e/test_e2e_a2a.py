"""The A2A service through nginx, as another agent sees it, with the official SDK."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import (
    CancelTaskRequest,
    ContentTypeNotSupportedError,
    DeleteTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTasksRequest,
    Message,
    Part,
    PushNotificationNotSupportedError,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskPushNotificationConfig,
    TaskState,
    UnsupportedOperationError,
)
from a2a.utils.errors import A2AError
from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict, ParseDict

#: Seconds a client waits for an answer; a scripted investigation takes a few.
TIMEOUT = 120.0

#: Every status message text is built by code from an event or a state: these are its
#: forms. A text of any other form could carry model text.
STATUS_TEXT = re.compile(
    r"investigation (queued|running|started \((alert|a2a)\)|interrupted"
    r"|interrupted by a restart|finished: (reported|degraded|failed)|failed: \w+)"
    r"|plan version \d+ \((model|default)\)"
    r"|step \d+ (started|done|failed|pending|running) \([a-z_]+\)"
    r"|evidence ev_[0-9a-f]{10} added \((alertmanager|kubernetes|git)(, [a-z0-9_]+)?\)"
    r"|round \d+ assessment: (sufficient|insufficient)"
    r"|report attempt \d+ rejected by [a-z_, ]+"
    r"|(degraded )?report published"
)


@asynccontextmanager
async def connect(url: str, token: str | None, *, streaming: bool = True) -> AsyncIterator[Client]:
    """An SDK client of the agent the card at ``url`` describes."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as http:
        card = await A2ACardResolver(http, url).get_agent_card()
        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=streaming))
        async with factory.create(card) as client:
            yield client


def data_part(value: Any) -> Part:
    return Part(data=ParseDict(value, struct_pb2.Value()))


def request(*parts: Part, task_id: str = "", immediately: bool = False) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            message_id=str(uuid.uuid4()), role=Role.ROLE_USER, parts=list(parts), task_id=task_id
        ),
        configuration=SendMessageConfiguration(return_immediately=immediately),
    )


def case(case_id: str, **options: Any) -> SendMessageRequest:
    return request(data_part({"case_id": case_id}), **options)


async def collect(stream: AsyncIterator[StreamResponse]) -> list[StreamResponse]:
    return [event async for event in stream]


async def start(url: str, token: str, message: SendMessageRequest) -> Task:
    """Send without streaming, so that ``returnImmediately`` answers at once."""
    async with connect(url, token, streaming=False) as client:
        [event] = await collect(client.send_message(message))
    return event.task


async def raises(error: type[A2AError], call: Any) -> str:
    with pytest.raises(error) as caught:
        await call
    return caught.value.message


def states(events: list[StreamResponse]) -> list[str]:
    seen = []
    for event in events:
        if event.HasField("task"):
            seen.append(TaskState.Name(event.task.status.state))
        elif event.HasField("status_update"):
            seen.append(TaskState.Name(event.status_update.status.state))
    return seen


def status_texts(events: list[StreamResponse]) -> list[str]:
    texts = []
    for event in events:
        if event.HasField("task"):
            parts = event.task.status.message.parts
        elif event.HasField("status_update"):
            parts = event.status_update.status.message.parts
        else:
            continue
        texts += [part.text for part in parts if part.HasField("text")]
    return texts


def data_of(parts: Any) -> list[Any]:
    return [MessageToDict(part.data) for part in parts if part.HasField("data")]


def test_the_agent_card_is_public_and_complete(url: str) -> None:
    response = httpx.get(f"{url}/.well-known/agent-card.json", timeout=TIMEOUT)
    assert response.status_code == 200
    card = response.json()
    for field in (
        "name",
        "description",
        "supportedInterfaces",
        "version",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ):
        assert card[field], field
    assert card["supportedInterfaces"] == [
        {"url": f"{url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    assert card["capabilities"] == {
        "streaming": True,
        "pushNotifications": False,
        "extendedAgentCard": False,
    }
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert card["defaultInputModes"] == ["application/json"]
    [skill] = card["skills"]
    assert skill["id"] == "investigate_alert"
    assert skill["name"] and skill["description"] and skill["tags"]


async def test_a_case_streams_to_a_report_equal_to_the_detail(url: str, token_a: str) -> None:
    async with connect(url, token_a) as client:
        events = await collect(client.send_message(case("s1-a")))
        task = events[0].task
        got = await client.get_task(GetTaskRequest(id=task.id))

    seen = states(events)
    assert seen[0] == "TASK_STATE_SUBMITTED" and seen[-1] == "TASK_STATE_COMPLETED"
    assert "TASK_STATE_WORKING" in seen
    for description in status_texts(events):
        assert STATUS_TEXT.fullmatch(description), description
    [artifact] = [e.artifact_update.artifact for e in events if e.HasField("artifact_update")]
    assert artifact.name == "report"
    [report] = data_of(artifact.parts)
    assert got.status.state == TaskState.TASK_STATE_COMPLETED
    assert data_of(got.artifacts[0].parts) == [report]

    investigation_id = report["investigation_id"]
    detail = httpx.get(f"{url}/api/investigations/{investigation_id}", timeout=TIMEOUT).json()
    assert report["report"] == detail["report"]["body"]
    assert report["report_status"] == detail["report"]["status"]
    assert [s["id"] for s in report["suggestions"]] == [s["id"] for s in detail["suggestions"]]
    assert report["detail_url"] == f"{url}/investigations/{investigation_id}"
    assert detail["investigation"]["trigger"] == "a2a"
    assert detail["investigation"]["caller"] == "e2e-agent-a"


async def test_a_client_that_reconnects_receives_the_end(url: str, token_a: str) -> None:
    # S1-d replays a log tool that times out and is retried: in scripted mode it takes
    # about six seconds, so the task is still working when the client reconnects.
    async with connect(url, token_a) as client:
        first: list[StreamResponse] = []
        stream = cast(AsyncGenerator[StreamResponse], client.send_message(case("s1-d")))
        async with aclosing(stream):
            async for event in stream:
                first.append(event)
                if states(first)[-1] == "TASK_STATE_WORKING":
                    break
        task_id = first[0].task.id
        again = await collect(client.subscribe(SubscribeToTaskRequest(id=task_id)))
        got = await client.get_task(GetTaskRequest(id=task_id))

    assert again[0].HasField("task") and again[0].task.id == task_id
    assert states(again)[0] == "TASK_STATE_WORKING"
    assert states(again)[-1] == "TASK_STATE_COMPLETED"
    for description in status_texts(again):
        assert STATUS_TEXT.fullmatch(description), description
    assert got.status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.parametrize("token", [None, "wrong-token"], ids=["no token", "wrong token"])
def test_a_request_without_a_valid_token_is_refused(url: str, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
    response = httpx.post(f"{url}/a2a", json=body, headers=headers, timeout=TIMEOUT)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_case_outside_the_clients_namespaces_is_rejected(url: str, token_b: str) -> None:
    async with connect(url, token_b) as client:
        events = await collect(client.send_message(case("s1-a")))
    assert states(events) == ["TASK_STATE_REJECTED"]
    [description] = status_texts(events)
    assert description.startswith("namespace_denied: ")


async def test_a_client_sees_only_its_own_tasks(url: str, token_a: str, token_b: str) -> None:
    theirs = await start(url, token_a, case("s1-b", immediately=True))
    async with connect(url, token_b) as client:
        for task_id in (theirs.id, "task_does_not_exist"):
            await raises(TaskNotFoundError, client.get_task(GetTaskRequest(id=task_id)))
            await raises(TaskNotFoundError, client.cancel_task(CancelTaskRequest(id=task_id)))
            await raises(
                TaskNotFoundError, collect(client.send_message(case("s1-a", task_id=task_id)))
            )
            await raises(
                TaskNotFoundError, collect(client.subscribe(SubscribeToTaskRequest(id=task_id)))
            )
        mine = [t.id for t in (await client.list_tasks(ListTasksRequest())).tasks]
    async with connect(url, token_a) as client:
        theirs_listed = [t.id for t in (await client.list_tasks(ListTasksRequest())).tasks]
    assert theirs.id not in mine
    assert theirs.id in theirs_listed


async def test_operations_bianque_does_not_offer(url: str, token_a: str) -> None:
    running = await start(url, token_a, case("s1-d", immediately=True))
    async with connect(url, token_a) as client:
        # While it runs: no further message, no cancelling, no push notifications.
        await raises(
            UnsupportedOperationError,
            collect(client.send_message(case("s1-a", task_id=running.id))),
        )
        await raises(TaskNotCancelableError, client.cancel_task(CancelTaskRequest(id=running.id)))
        config = TaskPushNotificationConfig(task_id=running.id, url="https://example.com/hook")
        for call in (
            client.create_task_push_notification_config(config),
            client.get_task_push_notification_config(
                GetTaskPushNotificationConfigRequest(task_id=running.id, id="c1")
            ),
            client.list_task_push_notification_configs(
                ListTaskPushNotificationConfigsRequest(task_id=running.id)
            ),
            client.delete_task_push_notification_config(
                DeleteTaskPushNotificationConfigRequest(task_id=running.id, id="c1")
            ),
        ):
            await raises(PushNotificationNotSupportedError, call)
        ended = await collect(client.subscribe(SubscribeToTaskRequest(id=running.id)))
        assert states(ended)[-1] == "TASK_STATE_COMPLETED"
        # Once it ended: still no further message and no cancelling.
        await raises(
            UnsupportedOperationError,
            collect(client.send_message(case("s1-a", task_id=running.id))),
        )
        await raises(TaskNotCancelableError, client.cancel_task(CancelTaskRequest(id=running.id)))
        listed = await client.list_tasks(ListTasksRequest())
        assert all(t.status.state != TaskState.TASK_STATE_INPUT_REQUIRED for t in listed.tasks)


async def test_free_text_is_not_supported_and_starts_no_task(url: str, token_a: str) -> None:
    async with connect(url, token_a) as client:
        before = (await client.list_tasks(ListTasksRequest())).total_size
        await raises(
            ContentTypeNotSupportedError,
            collect(client.send_message(request(Part(text="Why does s1-app restart?")))),
        )
        after = (await client.list_tasks(ListTasksRequest())).total_size
    assert after == before
