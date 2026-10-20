"""Memory across restarts and the replay profile's startup.

A restart is a second server process on the same database: everything the first one
knew is read back from PostgreSQL.
"""

from __future__ import annotations

import time
from typing import Any

from live_server import TIMEOUT, Frame, Server, read_frames, scripted, serve
from sqlalchemy import Engine, text


def ask(server: Server, session_id: int, question: str) -> list[Frame]:
    with server.client.stream(
        "POST", f"/api/sessions/{session_id}/messages", json={"question": question}
    ) as response:
        assert response.status_code == 200
        return list(read_frames(response.iter_lines()))


def snapshot(server: Server, investigation_id: str, session_id: int) -> dict[str, Any]:
    client = server.client
    return {
        "detail": client.get(f"/api/investigations/{investigation_id}").json(),
        "calls": client.get(f"/api/investigations/{investigation_id}/calls").json(),
        "session": client.get(f"/api/sessions/{session_id}").json(),
    }


def test_a_restart_keeps_the_investigation_and_the_conversation(
    migrated: Engine, pg_url: str
) -> None:
    with serve(scripted(pg_url)) as first:
        investigation_id = first.replay()
        first.wait_until(investigation_id, "reported")
        session_id = first.client.post(f"/api/investigations/{investigation_id}/sessions").json()[
            "id"
        ]
        assert ask(first, session_id, "Which commit broke it?")[-1].event == "done"
        before = snapshot(first, investigation_id, session_id)

    with serve(scripted(pg_url)) as second:
        # The detail, the calls and the conversation read back unchanged.
        assert snapshot(second, investigation_id, session_id) == before
        # The conversation continues on the original investigation's evidence.
        frames = ask(second, session_id, "What did it change?")
        session = second.client.get(f"/api/sessions/{session_id}").json()

    assert frames[-1].event == "done"
    evidence = {e["id"] for e in before["detail"]["evidence"]}
    suggestions = {s["id"] for s in before["detail"]["suggestions"]}
    cited = {c for f in frames if f.event == "content" for c in f.data["citations"]}
    assert cited and cited <= evidence | suggestions
    assert any(c in evidence for c in cited)
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"] * 2


def _investigations(migrated: Engine) -> list[tuple[str, str, str]]:
    with migrated.connect() as connection:
        rows = connection.execute(
            text("SELECT case_id, trigger, status FROM investigations ORDER BY case_id")
        )
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def _wait_for(migrated: Engine, count: int) -> list[tuple[str, str, str]]:
    deadline = time.monotonic() + TIMEOUT * 3
    while True:
        rows = _investigations(migrated)
        if len(rows) == count and all(status not in ("pending", "running") for *_, status in rows):
            return rows
        assert time.monotonic() < deadline, rows
        time.sleep(0.2)


def test_the_replay_startup_imports_knowledge_and_replays_every_case_once(
    migrated: Engine, pg_url: str
) -> None:
    app_settings = scripted(pg_url, replay_seed=True)
    cases = sorted(p.parent.parent.name for p in app_settings.cases_dir.glob("*/pack/alert.json"))
    assert len(cases) == 6
    with serve(app_settings):
        rows = _wait_for(migrated, len(cases))
    assert [case for case, *_ in rows] == cases
    assert {trigger for _, trigger, _ in rows} == {"replay"}
    assert {status for *_, status in rows} <= {"reported", "degraded"}
    with migrated.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM knowledge_chunks")) > 0

    # A second start finds every case replayed and starts nothing.
    with serve(app_settings) as server:
        time.sleep(1.0)
        listed = server.client.get("/api/investigations").json()["investigations"]
    assert len(listed) == len(cases) and len(_investigations(migrated)) == len(cases)
