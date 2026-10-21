"""The A2A service, seen by the official A2A SDK's client over HTTP.

Each test serves the application on a local port and talks to ``/a2a`` with the SDK's
JSON-RPC client, as another agent does. The replay profile (evidence packs) takes a
case to replay; the demo profile (``live`` tool backend) takes an Alertmanager alert.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from a2a.client import Client
from a2a.types import (
    CancelTaskRequest,
    ContentTypeNotSupportedError,
    DeleteTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTasksRequest,
    Part,
    PushNotificationNotSupportedError,
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
from a2a_client import (
    TIMEOUT,
    case,
    client_entry,
    collect,
    connect,
    data_of,
    data_part,
    message,
    start,
    states,
    task_of,
    texts_of,
)
from google.protobuf.json_format import MessageToJson
from live_server import CASES, EMBEDDING_FIXTURE, RUNBOOKS, Hold, Server, scripted, serve
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import bianque.agents.service as service_module
from bianque.config.settings import with_recorded_model
from bianque.knowledge.embedding import FixtureEmbedder
from bianque.knowledge.ingest import ingest
from bianque.llm import ScriptedReply
from bianque.llm.request import ModelRequest, request_key
from bianque.storage.db import session_factory
from bianque.tools.backends import open_pack
from bianque.tools.backends.base import ToolBackends

RECORDINGS = CASES.parent / "recordings"

#: A text no role rule writes; it marks model output that must not reach a client.
SENTINEL = "SENTINEL-a2a0c0de"

#: A write command the evidence gate rejects outside a change suggestion.
WRITE = "kubectl delete pod s1-app"

#: Every status message text is built by code from an event: these are its forms.
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

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED"}


def run[T](server: Server, token: str | None, work: Callable[[Client], Awaitable[T]]) -> T:
    """Connect an SDK client with ``token`` and run ``work`` with it."""

    async def go() -> T:
        async with connect(server.url, token) as client:
            return await work(client)

    return asyncio.run(go())


async def raises(error: type[A2AError], call: Awaitable[Any]) -> str:
    """Await ``call``, which must fail with ``error``; return the error's message."""
    with pytest.raises(error) as caught:
        await call
    return caught.value.message


async def stream_raises(error: type[A2AError], stream: AsyncIterator[Any]) -> None:
    """Read ``stream``, which must fail with ``error`` before it ends."""
    with pytest.raises(error):
        await collect(stream)


def count(engine: Engine, sql: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text(sql)) or 0)


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [dict(row._mapping) for row in connection.execute(text(sql))]


def status_text(task: Task) -> str:
    [description] = texts_of(task.status.message.parts)
    return description


def alert(status: str = "firing", **labels: str) -> dict[str, Any]:
    """The alert of the S1-a case, as one element of a webhook's alerts."""
    body = json.loads((CASES / "s1-a" / "pack" / "alert.json").read_text())
    item: dict[str, Any] = body["alerts"][0]
    item["status"] = status
    item["labels"].update(labels)
    return item


def assert_from_events(events: list[StreamResponse]) -> None:
    """Every status message of a stream is one code builds from an event or a state."""
    for event in events:
        if event.HasField("task"):
            status = event.task.status
        elif event.HasField("status_update"):
            status = event.status_update.status
        else:
            continue
        [description] = texts_of(status.message.parts)
        assert STATUS_TEXT.fullmatch(description), description


# Starting, streaming and the report ----------------------------------------------------


def test_a_case_streams_from_submitted_to_its_report(migrated: Engine, pg_url: str) -> None:
    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:

        async def work(client: Client) -> tuple[list[StreamResponse], Task, list[Task]]:
            events = await collect(client.send_message(case()))
            task = task_of(events)
            got = await client.get_task(GetTaskRequest(id=task.id))
            listed = await client.list_tasks(ListTasksRequest(include_artifacts=True))
            return events, got, list(listed.tasks)

        events, got, listed = run(server, token, work)

    seen = states(events)
    assert seen[0] == "TASK_STATE_SUBMITTED" and seen[-1] == "TASK_STATE_COMPLETED"
    assert set(seen[1:-1]) == {"TASK_STATE_WORKING"}
    assert_from_events(events)
    # One status update per progress event, in their order, then the artifact.
    updates = [e.status_update for e in events if e.HasField("status_update")]
    sequence = [data_of(u.status.message.parts)[0].get("seq") for u in updates[:-1]]
    assert sequence == list(range(1, len(updates)))
    assert events[-2].HasField("artifact_update")
    artifact = events[-2].artifact_update.artifact
    assert (artifact.artifact_id, artifact.name) == ("report", "report")

    [report] = data_of(artifact.parts)
    [summary] = texts_of(artifact.parts)
    investigation_id = report["investigation_id"]
    task = task_of(events)
    assert task.metadata["investigation_id"] == investigation_id
    assert got.status.state == TaskState.TASK_STATE_COMPLETED
    assert [a.name for a in got.artifacts] == ["report"]
    assert data_of(got.artifacts[0].parts) == [report]
    assert [t.id for t in listed] == [task.id]
    assert report["detail_url"] == f"http://127.0.0.1:8000/investigations/{investigation_id}"
    assert summary.startswith("Report: published\nSummary: ")
    assert summary.endswith(f"Details and confirmation: {report['detail_url']}")

    with serve(scripted(pg_url)) as reader:
        detail = reader.investigation(investigation_id)
    assert report["report"] == detail["report"]["body"]
    assert report["report_status"] == detail["report"]["status"] == "published"
    assert report["suggestions"] == [
        {key: s[key] for key in ("id", "action", "command", "risk", "state")}
        for s in detail["suggestions"]
    ]
    for suggestion in report["suggestions"]:
        assert suggestion["id"] in summary and suggestion["action"] in summary
    assert detail["investigation"]["trigger"] == "a2a"
    assert detail["investigation"]["caller"] == "agent-a"
    assert detail["report"]["body"]["summary"] in summary


def test_a_task_records_its_investigation_context_and_caller(migrated: Engine, pg_url: str) -> None:
    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:
        events = run(
            server, token, lambda c: collect(c.send_message(case(context_id="ctx-of-the-client")))
        )
        task = task_of(events)
        investigation_id = task.metadata["investigation_id"]
        server.wait_until(investigation_id, "reported")

    [row] = rows(migrated, "SELECT * FROM a2a_tasks")
    assert row["id"] == task.id and row["id"].startswith("task_")
    assert (row["context_id"], task.context_id) == ("ctx-of-the-client", "ctx-of-the-client")
    assert (row["investigation_id"], row["caller"]) == (investigation_id, "agent-a")
    assert row["rejected_reason"] is None
    [investigation] = rows(migrated, "SELECT trigger, case_id FROM investigations")
    assert investigation == {"trigger": "a2a", "case_id": "s1-a"}
    started = rows(
        migrated,
        "SELECT payload FROM investigation_events WHERE type = 'investigation_started'",
    )
    assert started == [{"payload": {"trigger": "a2a"}}]


def test_a_blocking_message_returns_the_ended_task(migrated: Engine, pg_url: str) -> None:
    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:

        async def work(client: Client) -> tuple[Task, Task]:
            async with connect(server.url, token, streaming=False) as blocking:
                [ended] = await collect(blocking.send_message(case()))
                [early] = await collect(blocking.send_message(case(return_immediately=True)))
            return ended.task, early.task

        ended, early = run(server, token, work)
        server.wait_until(early.metadata["investigation_id"], "reported")

    assert ended.status.state == TaskState.TASK_STATE_COMPLETED
    assert [a.name for a in ended.artifacts] == ["report"]
    assert early.status.state in (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING)
    assert not early.artifacts


def test_the_same_case_makes_the_same_model_requests_as_a_replay(
    migrated: Engine, pg_url: str
) -> None:
    keys: list[str] = []

    def record(request: ModelRequest) -> ScriptedReply | None:
        keys.append(request_key(request))
        return None

    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry]), rules=[record]) as server:
        server.wait_until(server.replay("s1-a"), "reported")
        replayed = Counter(keys)
        keys.clear()
        events = run(server, token, lambda c: collect(c.send_message(case("s1-a"))))
        assert states(events)[-1] == "TASK_STATE_COMPLETED"
        through_a2a = Counter(keys)

    assert replayed and through_a2a == replayed


def test_a_case_started_through_a2a_hits_every_recording(migrated: Engine, pg_url: str) -> None:
    async def import_runbooks() -> None:
        engine = create_async_engine(pg_url, poolclass=NullPool)
        try:
            embedder = FixtureEmbedder(EMBEDDING_FIXTURE)
            await ingest(RUNBOOKS, sessions=session_factory(engine), embedder=embedder)
        finally:
            await engine.dispose()

    asyncio.run(import_runbooks())
    token, entry = client_entry("agent-a")
    settings = with_recorded_model(
        scripted(pg_url, llm_mode="recorded", llm_recordings_dir=RECORDINGS, a2a_clients=[entry])
    )
    with serve(settings) as server:
        events = run(server, token, lambda c: collect(c.send_message(case("s1-a"))))
        detail = server.investigation(task_of(events).metadata["investigation_id"])

    assert states(events)[-1] == "TASK_STATE_COMPLETED"
    assert (detail["investigation"]["status"], detail["investigation"]["error"]) == (
        "reported",
        None,
    )
    assert detail["investigation"]["llm_mode"] == "recorded"


def test_a_failed_investigation_fails_the_task(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    token, entry = client_entry("agent-a")
    settings = scripted(
        pg_url, llm_mode="recorded", llm_recordings_dir=tmp_path, a2a_clients=[entry]
    )
    with serve(settings) as server:
        events = run(server, token, lambda c: collect(c.send_message(case())))

    final = events[-1].status_update.status
    assert TaskState.Name(final.state) == "TASK_STATE_FAILED"
    assert texts_of(final.message.parts) == ["investigation failed: RecordingNotFoundError"]
    assert data_of(final.message.parts) == [
        {"reason": "investigation_failed", "error": "RecordingNotFoundError"}
    ]
    assert not any(e.HasField("artifact_update") for e in events)


def test_status_messages_carry_no_model_text(migrated: Engine, pg_url: str) -> None:
    """The plan carries a write command twice; the gate rejects it, and no text escapes."""

    def bad_plan(request: ModelRequest) -> ScriptedReply | None:
        if request.operation != "plan":
            return None
        plan = {
            "rationale": f"{WRITE} first. {SENTINEL}",
            "steps": [
                {
                    "goal": f"Read the pod. {SENTINEL}",
                    "domain": "kubernetes",
                    "suggested_tools": ["k8s_get_pod_status"],
                    "expected_evidence": "Pod status.",
                }
            ],
            "knowledge_refs": [],
        }
        return ScriptedReply(tool_calls=(("submit_plan", plan),))

    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry]), rules=[bad_plan]) as server:

        async def work(client: Client) -> list[str]:
            events = await collect(client.send_message(case()))
            task = task_of(events)
            got = await client.get_task(GetTaskRequest(id=task.id))
            listed = await client.list_tasks(ListTasksRequest(include_artifacts=True))
            return [MessageToJson(e) for e in events] + [MessageToJson(got), MessageToJson(listed)]

        seen = run(server, token, work)

    assert any('"plan version 1 (default)"' in item for item in seen)
    assert all(SENTINEL not in item for item in seen)


# Authentication and ownership ---------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-token"}, {"Authorization": "Basic YTpi"}],
    ids=["no token", "wrong token", "not bearer"],
)
def test_a_request_without_a_valid_token_is_refused(
    migrated: Engine, pg_url: str, headers: dict[str, str]
) -> None:
    _, entry = client_entry("agent-a")
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "task_x"}}
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:
        response = httpx.post(f"{server.url}/a2a", json=body, headers=headers, timeout=TIMEOUT)
        card = httpx.get(f"{server.url}/.well-known/agent-card.json", timeout=TIMEOUT)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "a valid bearer token is required"}
    assert card.status_code == 200 and card.json()["name"] == "Bianque"


def test_the_sdk_client_is_refused_a_wrong_token(migrated: Engine, pg_url: str) -> None:
    _, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:

        async def work(client: Client) -> None:
            with pytest.raises(Exception, match="401"):
                await collect(client.send_message(case()))

        run(server, "wrong-token", work)
    assert count(migrated, "SELECT count(*) FROM a2a_tasks") == 0


def test_without_clients_every_request_is_refused(migrated: Engine, pg_url: str) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "task_x"}}
    with serve(scripted(pg_url)) as server:
        response = httpx.post(
            f"{server.url}/a2a", json=body, headers={"Authorization": "Bearer x"}, timeout=TIMEOUT
        )
        card = httpx.get(f"{server.url}/.well-known/agent-card.json", timeout=TIMEOUT)
    assert response.status_code == 401
    assert card.status_code == 200


def test_a_client_sees_only_its_own_tasks(migrated: Engine, pg_url: str) -> None:
    token_a, entry_a = client_entry("agent-a")
    token_b, entry_b = client_entry("agent-b")
    with serve(scripted(pg_url, a2a_clients=[entry_a, entry_b])) as server:
        theirs = asyncio.run(start(server.url, token_b, case(return_immediately=True)))

        async def probe(client: Client) -> dict[str, list[str]]:
            messages: dict[str, list[str]] = {}
            for task_id in (theirs.id, "task_does_not_exist"):
                messages[task_id] = [
                    await raises(TaskNotFoundError, client.get_task(GetTaskRequest(id=task_id))),
                    await raises(
                        TaskNotFoundError, client.cancel_task(CancelTaskRequest(id=task_id))
                    ),
                    await raises(
                        TaskNotFoundError,
                        collect(client.send_message(case(task_id=task_id))),
                    ),
                ]
                await stream_raises(
                    TaskNotFoundError, client.subscribe(SubscribeToTaskRequest(id=task_id))
                )
            mine = await start(server.url, token_a, case(return_immediately=True))
            listed = await client.list_tasks(ListTasksRequest())
            assert [t.id for t in listed.tasks] == [mine.id]
            return messages

        messages = run(server, token_a, probe)
        listed_b = run(server, token_b, lambda c: c.list_tasks(ListTasksRequest()))
        for item in rows(migrated, "SELECT investigation_id FROM a2a_tasks"):
            server.wait_until(item["investigation_id"], "reported")

    # Another client's task and a task that does not exist cannot be told apart.
    assert messages[theirs.id] == messages["task_does_not_exist"]
    assert [t.id for t in listed_b.tasks] == [theirs.id]


# Validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parts",
    [
        [Part(text="Why does s1-app restart?")],
        [Part(text="Please look at this."), data_part({"case_id": "s1-a"})],
        [Part(url="https://example.com/alert.json")],
    ],
    ids=["text", "text and data", "url"],
)
def test_content_other_than_data_is_not_supported(
    migrated: Engine, pg_url: str, parts: list[Part]
) -> None:
    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:

        async def work(client: Client) -> None:
            await raises(
                ContentTypeNotSupportedError, collect(client.send_message(message(*parts)))
            )
            assert not (await client.list_tasks(ListTasksRequest())).tasks

        run(server, token, work)
    assert count(migrated, "SELECT count(*) FROM a2a_tasks") == 0
    assert count(migrated, "SELECT count(*) FROM investigations") == 0


@pytest.mark.parametrize(
    ("backend", "parts", "namespaces", "reason"),
    [
        (
            "pack",
            [data_part({"case_id": "s1-a"}), data_part({"case_id": "s1-b"})],
            None,
            "malformed",
        ),
        ("pack", [data_part(["s1-a"])], None, "malformed"),
        ("pack", [data_part({"case": "s1-a"})], None, "malformed"),
        ("pack", [data_part({"case_id": "s1-a", "pod": "x"})], None, "malformed"),
        ("pack", [data_part({"case_id": 7})], None, "malformed"),
        ("pack", [data_part({"case_id": "s1-a"})], ["other"], "namespace_denied"),
        ("pack", [data_part(alert())], None, "profile_mismatch"),
        ("pack", [data_part({"case_id": "s9-z"})], None, "unknown_case"),
        ("pack", [data_part({"case_id": "../s1-a"})], None, "unknown_case"),
        ("live", [data_part({"case_id": "s1-a"})], None, "profile_mismatch"),
        ("live", [data_part({"labels": {"namespace": "demo-s1"}})], None, "malformed"),
        ("live", [data_part(alert(namespace="kube-system"))], None, "namespace_denied"),
        ("live", [data_part(alert("resolved"))], None, "resolved_alert"),
        ("live", [data_part(alert("resolved", namespace="other"))], None, "namespace_denied"),
    ],
)
def test_a_request_that_cannot_be_served_is_rejected(
    migrated: Engine,
    pg_url: str,
    backend: str,
    parts: list[Part],
    namespaces: list[str] | None,
    reason: str,
) -> None:
    token, entry = client_entry("agent-a", namespaces)
    with serve(scripted(pg_url, tool_backend=backend, a2a_clients=[entry])) as server:

        async def work(client: Client) -> tuple[list[StreamResponse], Task]:
            events = await collect(client.send_message(message(*parts)))
            return events, await client.get_task(GetTaskRequest(id=task_of(events).id))

        events, got = run(server, token, work)

    assert states(events) == ["TASK_STATE_REJECTED"]
    for task in (task_of(events), got):
        assert task.status.state == TaskState.TASK_STATE_REJECTED
        assert status_text(task).startswith(f"{reason}: ")
        assert data_of(task.status.message.parts) == [{"reason": reason}]
        assert "investigation_id" not in task.metadata
    [row] = rows(migrated, "SELECT investigation_id, rejected_reason, caller FROM a2a_tasks")
    assert row == {"investigation_id": None, "rejected_reason": reason, "caller": "agent-a"}
    assert count(migrated, "SELECT count(*) FROM investigations") == 0
    assert count(migrated, "SELECT count(*) FROM alerts") == 0


@asynccontextmanager
async def _s1a_pack(settings: Any, *, case: str | None = None) -> AsyncIterator[ToolBackends]:
    async with open_pack(CASES / "s1-a" / "pack") as backends:
        yield backends


@pytest.fixture
def cluster_from_pack(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Investigations of the demo profile read the S1-a pack instead of a cluster."""
    monkeypatch.setattr(service_module, "open_backends", _s1a_pack)
    yield


def test_an_alert_under_investigation_joins_that_investigation(
    migrated: Engine, pg_url: str, cluster_from_pack: None
) -> None:
    token, entry = client_entry("agent-a")
    hold = Hold("investigation")
    settings = scripted(pg_url, tool_backend="live", a2a_clients=[entry])
    with serve(settings, rules=[hold.rule], model_sleep=hold.sleep) as server:

        async def work(client: Client) -> list[Task]:
            request = message(data_part(alert()), return_immediately=True)
            first = await start(server.url, token, request)
            await asyncio.to_thread(hold.wait_reached)
            second = await start(server.url, token, request)
            hold.released.set()
            events = await collect(client.subscribe(SubscribeToTaskRequest(id=second.id)))
            assert states(events)[0] == "TASK_STATE_WORKING"
            assert states(events)[-1] == "TASK_STATE_COMPLETED"
            return [first, second] + [
                await client.get_task(GetTaskRequest(id=t.id)) for t in (first, second)
            ]

        first, second, first_now, second_now = run(server, token, work)
        investigation_id = first.metadata["investigation_id"]
        detail = server.investigation(investigation_id)

    assert second.metadata["investigation_id"] == investigation_id
    assert second.status.state == TaskState.TASK_STATE_WORKING
    assert first_now.status.state == second_now.status.state == TaskState.TASK_STATE_COMPLETED
    assert count(migrated, "SELECT count(*) FROM investigations") == 1
    assert count(migrated, "SELECT count(*) FROM alerts") == 1
    assert detail["investigation"]["trigger"] == "a2a"
    assert detail["investigation"]["caller"] == "agent-a"
    assert detail["investigation"]["case_id"] is None


# Progress across a reconnection, and a restart ----------------------------------------


def test_a_client_that_reconnects_follows_the_task_to_its_end(
    migrated: Engine, pg_url: str
) -> None:
    token, entry = client_entry("agent-a")
    hold = Hold("kubernetes", "logs")
    with serve(
        scripted(pg_url, a2a_clients=[entry]), rules=[hold.rule], model_sleep=hold.sleep
    ) as server:

        async def work(client: Client) -> tuple[list[StreamResponse], Task, list[StreamResponse]]:
            first: list[StreamResponse] = []
            stream = cast(AsyncGenerator[StreamResponse], client.send_message(case()))
            async with aclosing(stream):
                async for event in stream:
                    first.append(event)
                    if states(first)[-1:] == ["TASK_STATE_WORKING"]:
                        break
            await asyncio.to_thread(hold.wait_reached)
            task = task_of(first)
            running = await client.get_task(GetTaskRequest(id=task.id))
            hold.released.set()
            again = await collect(client.subscribe(SubscribeToTaskRequest(id=task.id)))
            await raises(
                UnsupportedOperationError,
                collect(client.subscribe(SubscribeToTaskRequest(id=task.id))),
            )
            return first, running, again

        first, running, again = run(server, token, work)

    assert running.status.state == TaskState.TASK_STATE_WORKING
    assert_from_events(again)
    assert again[0].HasField("task") and again[0].task.id == task_of(first).id
    assert states(again)[0] == "TASK_STATE_WORKING" and states(again)[-1] == "TASK_STATE_COMPLETED"
    assert [e.artifact_update.artifact.name for e in again if e.HasField("artifact_update")] == [
        "report"
    ]
    # The resumed stream continues after the events its first task reflects.
    sequence = [
        data_of(e.status_update.status.message.parts)[0]["seq"]
        for e in again[1:]
        if e.HasField("status_update") and "seq" in data_of(e.status_update.status.message.parts)[0]
    ]
    assert sequence == sorted(sequence) and len(set(sequence)) == len(sequence)


def test_a_task_whose_investigation_was_interrupted_fails(migrated: Engine, pg_url: str) -> None:
    token, entry = client_entry("agent-a")
    hold = Hold("investigation")
    settings = scripted(pg_url, a2a_clients=[entry])
    with serve(settings, rules=[hold.rule], model_sleep=hold.sleep) as first:
        task = asyncio.run(start(first.url, token, case(return_immediately=True)))
        hold.wait_reached()

    with serve(settings) as second:

        async def work(client: Client) -> Task:
            await raises(
                UnsupportedOperationError,
                collect(client.subscribe(SubscribeToTaskRequest(id=task.id))),
            )
            return await client.get_task(GetTaskRequest(id=task.id))

        got = run(second, token, work)

    assert got.status.state == TaskState.TASK_STATE_FAILED
    assert status_text(got) == "investigation interrupted by a restart"
    assert data_of(got.status.message.parts) == [{"reason": "investigation_interrupted"}]


# Operations Bianque does not offer ----------------------------------------------------


def test_operations_bianque_does_not_offer_are_refused(migrated: Engine, pg_url: str) -> None:
    token, entry = client_entry("agent-a")
    hold = Hold("investigation")
    with serve(
        scripted(pg_url, a2a_clients=[entry]), rules=[hold.rule], model_sleep=hold.sleep
    ) as server:

        async def work(client: Client) -> None:
            running = await start(server.url, token, case(return_immediately=True))
            await asyncio.to_thread(hold.wait_reached)
            config = TaskPushNotificationConfig(task_id=running.id, url="https://example.com/hook")
            await raises(
                PushNotificationNotSupportedError,
                client.create_task_push_notification_config(config),
            )
            await raises(
                PushNotificationNotSupportedError,
                client.get_task_push_notification_config(
                    GetTaskPushNotificationConfigRequest(task_id=running.id, id="c1")
                ),
            )
            await raises(
                PushNotificationNotSupportedError,
                client.list_task_push_notification_configs(
                    ListTaskPushNotificationConfigsRequest(task_id=running.id)
                ),
            )
            await raises(
                PushNotificationNotSupportedError,
                client.delete_task_push_notification_config(
                    DeleteTaskPushNotificationConfigRequest(task_id=running.id, id="c1")
                ),
            )
            await raises(
                TaskNotCancelableError, client.cancel_task(CancelTaskRequest(id=running.id))
            )
            await raises(
                UnsupportedOperationError,
                collect(client.send_message(case(task_id=running.id))),
            )
            # The SDK's client does not ask for an extended card the Agent Card denies.
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                answer = await http.post(
                    f"{server.url}/a2a",
                    json={"jsonrpc": "2.0", "id": 1, "method": "GetExtendedAgentCard"},
                    headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
                )
            assert answer.json()["error"]["code"] == -32004
            hold.released.set()
            ended = await collect(client.subscribe(SubscribeToTaskRequest(id=running.id)))
            assert states(ended)[-1] == "TASK_STATE_COMPLETED"
            await raises(
                TaskNotCancelableError, client.cancel_task(CancelTaskRequest(id=running.id))
            )
            await raises(
                UnsupportedOperationError,
                collect(client.send_message(case(task_id=running.id))),
            )
            listed = await client.list_tasks(ListTasksRequest())
            assert [t.id for t in listed.tasks] == [running.id]
            assert all(t.status.state != TaskState.TASK_STATE_INPUT_REQUIRED for t in listed.tasks)

        run(server, token, work)
    assert count(migrated, "SELECT count(*) FROM investigations") == 1


def test_tasks_are_listed_newest_first_and_paged(migrated: Engine, pg_url: str) -> None:
    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:

        async def work(client: Client) -> tuple[list[str], list[str], list[str], list[str]]:
            made = [
                task_of(await collect(client.send_message(message(data_part(["bad"]))))).id
                for _ in range(3)
            ]
            context = await start(
                server.url, token, case(context_id="ctx-1", return_immediately=True)
            )
            first = await client.list_tasks(ListTasksRequest(page_size=2))
            second = await client.list_tasks(
                ListTasksRequest(page_size=2, page_token=first.next_page_token)
            )
            rejected = await client.list_tasks(
                ListTasksRequest(status=TaskState.TASK_STATE_REJECTED)
            )
            in_context = await client.list_tasks(ListTasksRequest(context_id="ctx-1"))
            assert first.total_size == 4 and second.next_page_token == ""
            return (
                made + [context.id],
                [t.id for t in first.tasks] + [t.id for t in second.tasks],
                [t.id for t in rejected.tasks],
                [t.id for t in in_context.tasks],
            )

        made, paged, rejected, in_context = run(server, token, work)
        for item in rows(migrated, "SELECT investigation_id FROM a2a_tasks"):
            if item["investigation_id"]:
                server.wait_until(item["investigation_id"], "reported")

    assert paged == made[::-1]
    assert rejected == made[:3][::-1]
    assert in_context == [made[3]]


def test_the_request_make_replay_prints_starts_a_task(migrated: Engine, pg_url: str) -> None:
    makefile = (Path(__file__).resolve().parents[3] / "Makefile").read_text()
    printed = re.search(r"^A2A_EXAMPLE := (.*?)\n\n", makefile, re.MULTILINE | re.DOTALL)
    assert printed, "the Makefile defines A2A_EXAMPLE"
    body = json.loads(re.sub(r"\\\n\t", "", printed[1]))
    token, entry = client_entry("agent-a")
    with serve(scripted(pg_url, a2a_clients=[entry])) as server:
        response = httpx.post(
            f"{server.url}/a2a",
            json=body,
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
            timeout=TIMEOUT,
        )
        task = response.json()["result"]["task"]
        server.wait_until(task["metadata"]["investigation_id"], "reported")
    assert task["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")
