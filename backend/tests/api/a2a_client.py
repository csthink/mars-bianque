"""A2A clients of the official SDK, for tests that talk to a served application.

The Agent Card of a test server announces ``BIANQUE_A2A_PUBLIC_URL``, configured before
the server picks its port; the client is pointed at the server's ``/a2a`` instead.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskState,
)
from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict, ParseDict

from bianque.auth.bearer import token_sha256

#: Seconds a client waits for an answer.
TIMEOUT = 60.0

#: The namespace of the cases' alerts.
NAMESPACE = "demo-s1"


def client_entry(name: str, namespaces: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    """A fresh token and the configuration entry of an A2A client holding it."""
    token = secrets.token_hex(16)
    return token, {
        "name": name,
        "token_sha256": token_sha256(token),
        "namespaces": namespaces or [NAMESPACE],
    }


@asynccontextmanager
async def connect(url: str, token: str | None, *, streaming: bool = True) -> AsyncIterator[Client]:
    """An SDK client of the server at ``url``, sending ``token`` as its bearer token."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as http:
        card = await A2ACardResolver(http, url).get_agent_card()
        card.supported_interfaces[0].url = f"{url}/a2a"
        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=streaming))
        async with factory.create(card) as client:
            yield client


async def start(url: str, token: str, request: SendMessageRequest) -> Task:
    """Send ``request`` with a client that does not stream; return the task it answers with.

    Only a message that is not streamed returns before the task ends when it asks for
    ``returnImmediately``; a stream always runs to the end of the task.
    """
    async with connect(url, token, streaming=False) as client:
        [event] = await collect(client.send_message(request))
    return event.task


def data_part(value: Any) -> Part:
    """A data part holding ``value``."""
    return Part(data=ParseDict(value, struct_pb2.Value()))


def message(
    *parts: Part,
    task_id: str = "",
    context_id: str = "",
    return_immediately: bool = False,
) -> SendMessageRequest:
    """A message from the client."""
    return SendMessageRequest(
        message=Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=list(parts),
            task_id=task_id,
            context_id=context_id,
        ),
        configuration=SendMessageConfiguration(return_immediately=return_immediately),
    )


def case(case_id: str = "s1-a", **options: Any) -> SendMessageRequest:
    """A message asking to replay a case."""
    return message(data_part({"case_id": case_id}), **options)


async def collect(stream: AsyncIterator[StreamResponse]) -> list[StreamResponse]:
    """Every event of a stream."""
    return [event async for event in stream]


def task_of(events: list[StreamResponse]) -> Task:
    """The task an event stream starts with."""
    assert events and events[0].HasField("task"), events[:1]
    return events[0].task


def states(events: list[StreamResponse]) -> list[str]:
    """The task states an event stream reports, in order, by name."""
    seen = []
    for event in events:
        if event.HasField("task"):
            seen.append(TaskState.Name(event.task.status.state))
        elif event.HasField("status_update"):
            seen.append(TaskState.Name(event.status_update.status.state))
    return seen


def data_of(parts: Any) -> list[Any]:
    """The contents of the data parts among ``parts``."""
    return [MessageToDict(part.data) for part in parts if part.HasField("data")]


def texts_of(parts: Any) -> list[str]:
    """The texts of the text parts among ``parts``."""
    return [part.text for part in parts if part.HasField("text")]
