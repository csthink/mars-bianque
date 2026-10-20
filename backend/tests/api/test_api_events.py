"""The progress event stream: live delivery, resumption and the end of the stream."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest
from live_server import Frame, Hold, Server, read_frames, scripted, serve
from sqlalchemy import Engine

from bianque.agents.events import TERMINAL_EVENTS
from bianque.api import investigations


def frames(
    server: Server,
    investigation_id: str,
    params: dict[str, int] | None = None,
    last_event_id: str | None = None,
) -> Iterator[Frame]:
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    with server.client.stream(
        "GET", f"/api/investigations/{investigation_id}/events", params=params, headers=headers
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        yield from read_frames(response.iter_lines())


def test_events_arrive_while_the_investigation_runs(migrated: Engine, pg_url: str) -> None:
    hold = Hold("kubernetes", "logs")
    with serve(scripted(pg_url), rules=[hold.rule], model_sleep=hold.sleep) as server:
        investigation_id = server.replay()
        stream = frames(server, investigation_id)
        seen: list[Frame] = []
        for frame in stream:
            seen.append(frame)
            if frame.event == "plan_created":
                break
        # The sub-agents are held: the investigation runs and has not finished.
        hold.wait_reached()
        assert server.investigation(investigation_id)["investigation"]["status"] == "running"
        hold.released.set()
        seen += list(stream)

    events = [f.event for f in seen]
    assert events[:3] == ["investigation_started", "evidence_added", "plan_created"]
    assert events[-1] == "investigation_finished" and seen[-1].data["status"] == "reported"
    assert "step_started" in events and "report_published" in events
    assert [int(f.id or 0) for f in seen] == list(range(1, len(seen) + 1))
    assert [f.data["seq"] for f in seen] == list(range(1, len(seen) + 1))
    assert all(f.data["type"] == f.event for f in seen)


def test_a_finished_investigation_sends_everything_and_closes(
    migrated: Engine, pg_url: str
) -> None:
    with serve(scripted(pg_url)) as server:
        investigation_id = server.replay()
        server.wait_until(investigation_id, "reported")
        everything = list(frames(server, investigation_id))
        resumed = list(frames(server, investigation_id, last_event_id="3"))
        after = server.client.get(f"/api/investigations/{investigation_id}/events?after=5")
        both = list(frames(server, investigation_id, {"after": 2}, last_event_id="4"))
    assert everything[-1].event in TERMINAL_EVENTS
    assert [f.data for f in resumed] == [f.data for f in everything[3:]]
    assert [f.data for f in read_frames(iter(after.text.splitlines()))] == [
        f.data for f in everything[5:]
    ]
    assert [f.data for f in both] == [f.data for f in everything[4:]]


def test_a_malformed_last_event_id_starts_from_the_beginning(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        investigation_id = server.replay()
        server.wait_until(investigation_id, "reported")
        resumed = list(frames(server, investigation_id, last_event_id="x"))
    assert resumed[0].data["seq"] == 1


def test_an_unknown_investigation_has_no_stream(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        response = server.client.get("/api/investigations/inv_missing/events")
    assert response.status_code == 404


def test_a_reader_is_woken_by_new_events(
    migrated: Engine, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events are pushed as they are stored, not found at the next read of the table.

    The stream reads the table again only every 30 seconds here, so the events after
    the release arrive in time only if the writer wakes the stream up.
    """
    monkeypatch.setattr(investigations, "EVENT_POLL_SECONDS", 30.0)
    hold = Hold("kubernetes", "logs")
    with serve(scripted(pg_url), rules=[hold.rule], model_sleep=hold.sleep) as server:
        investigation_id = server.replay()
        hold.wait_reached()
        stream = frames(server, investigation_id)
        for frame in stream:
            if frame.event == "plan_created":
                break
        released = time.monotonic()
        hold.released.set()
        rest = list(stream)
    assert rest[-1].event == "investigation_finished"
    assert rest[-1].received_at - released < 15.0


def test_the_stream_needs_no_login(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        investigation_id = server.replay()
        server.wait_until(investigation_id, "reported")
        anonymous = httpx.Client(base_url=server.url)
        response = anonymous.get(f"/api/investigations/{investigation_id}/events")
    assert response.status_code == 200
