"""Manual triggers, follow-up sessions and the follow-up answer stream."""

from __future__ import annotations

from typing import Any

import httpx
import yaml
from demo_login import USERNAME
from live_server import CASES, Frame, Hold, Server, read_frames, scripted, serve
from sqlalchemy import Engine, func, select, text

from bianque.llm import ScriptedReply
from bianque.llm.request import ModelRequest
from bianque.storage.models import FollowupMessage

#: Marks model text that must never reach the client.
SENTINEL = "SENTINEL-57ream"


def finished(server: Server, case: str = "s1-a") -> str:
    investigation_id = server.replay(case)
    server.wait_until(investigation_id, "reported", "degraded")
    return investigation_id


def open_session(server: Server, investigation_id: str) -> int:
    response = server.client.post(f"/api/investigations/{investigation_id}/sessions")
    assert response.status_code == 201, response.text
    session_id: int = response.json()["id"]
    return session_id


def ask_raw(server: Server, session_id: int, question: str) -> tuple[int, bytes]:
    """The status and the complete raw body of a question's answer stream."""
    with server.client.stream(
        "POST", f"/api/sessions/{session_id}/messages", json={"question": question}
    ) as response:
        return response.status_code, response.read()


def frames_of(body: bytes) -> list[Frame]:
    return list(read_frames(iter(body.decode().splitlines())))


def test_writes_need_a_login(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        investigation_id = finished(server)
        session_id = open_session(server, investigation_id)
        anonymous = httpx.Client(base_url=server.url)
        assert anonymous.post("/api/investigations", json={"case_id": "s1-a"}).status_code == 401
        sessions = f"/api/investigations/{investigation_id}/sessions"
        assert anonymous.post(sessions).status_code == 401
        message = {"question": "Why?"}
        assert (
            anonymous.post(f"/api/sessions/{session_id}/messages", json=message).status_code == 401
        )
        # Reading needs no login.
        assert anonymous.get(f"/api/sessions/{session_id}").status_code == 200
        assert anonymous.get(f"/api/investigations/{investigation_id}/questions").status_code == 200


def test_a_case_and_an_earlier_alert_can_be_investigated_by_hand(
    migrated: Engine, pg_url: str
) -> None:
    with serve(scripted(pg_url)) as server:
        replayed = finished(server)
        first = server.investigation(replayed)
        assert first["investigation"]["trigger"] == "replay"
        alert_id = _alert_id(migrated, replayed)

        again = server.client.post("/api/investigations", json={"alert_id": alert_id})
        assert again.status_code == 202
        detail = server.wait_until(again.json()["investigation_id"], "reported")
        assert detail["investigation"]["trigger"] == "manual"
        assert detail["alert"]["fingerprint"] == first["alert"]["fingerprint"]

        post = server.client.post
        assert post("/api/investigations", json={}).status_code == 422
        both = {"case_id": "s1-a", "alert_id": alert_id}
        assert post("/api/investigations", json=both).status_code == 422
        assert post("/api/investigations", json={"case_id": "s9-z"}).status_code == 404
        assert post("/api/investigations", json={"case_id": "../s1-a"}).status_code == 404
        assert post("/api/investigations", json={"alert_id": 999999}).status_code == 404


def _alert_id(migrated: Engine, investigation_id: str) -> int:
    with migrated.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT alert_id FROM investigations WHERE id = :id"), {"id": investigation_id}
            )
        )


def test_a_case_needs_the_pack_backend(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url, tool_backend="live")) as server:
        response = server.client.post("/api/investigations", json={"case_id": "s1-a"})
    assert response.status_code == 409


def test_an_answer_is_released_after_validation_and_kept(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        session_id = open_session(server, finished(server))
        status, body = ask_raw(server, session_id, "Why this commit?")
        detail = server.client.get(f"/api/sessions/{session_id}").json()
    assert status == 200
    frames = frames_of(body)
    events = [f.event for f in frames]
    assert events[0] == "progress" and events[-1] == "done"
    last_validating = max(
        i for i, f in enumerate(frames) if f.event == "progress" and f.data["stage"] == "validating"
    )
    assert events.index("content") > last_validating
    assert all(set(f.data) == {"stage", "elapsed_ms"} for f in frames if f.event == "progress")
    contents = [f.data for f in frames if f.event == "content"]
    assert [c["segment_index"] for c in contents] == list(range(len(contents)))
    assert frames[-1].data == {"message_id": detail["messages"][1]["id"], "validated": True}
    question, answer = detail["messages"]
    assert question["role"] == "user" and question["content"] == "Why this commit?"
    assert [s["text"] for s in answer["segments"]] == [c["text"] for c in contents]
    assert [s["citations"] for s in answer["segments"]] == [c["citations"] for c in contents]


def test_a_rejected_answer_streams_no_model_text(migrated: Engine, pg_url: str) -> None:
    """A failing answer's whole event stream holds no model text."""

    def always_rejected(request: ModelRequest) -> ScriptedReply | None:
        if request.agent != "followup":
            return None
        segments = [
            {
                "text": f"{SENTINEL} one",
                "citations": ["ev_ffffffffff"],
                "insufficient_evidence": False,
            },
            {"text": f"{SENTINEL} two", "citations": [], "insufficient_evidence": False},
        ]
        return ScriptedReply(
            tool_calls=(("submit_answer", {"segments": segments, "references": []}),)
        )

    with serve(scripted(pg_url), rules=[always_rejected]) as server:
        session_id = open_session(server, finished(server))
        status, body = ask_raw(server, session_id, "Why?")
        detail = server.client.get(f"/api/sessions/{session_id}").json()
    assert status == 200
    assert SENTINEL.encode() not in body
    frames = frames_of(body)
    assert {f.event for f in frames} == {"progress", "failed"}
    assert frames[-1].data == {"reason": "gate_rejected", "violations": ["citation_integrity"]}
    assert detail["messages"] == []
    with migrated.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(FollowupMessage)) == 0


def test_a_session_needs_a_finished_investigation(migrated: Engine, pg_url: str) -> None:
    hold = Hold("investigation")
    with serve(scripted(pg_url), rules=[hold.rule], model_sleep=hold.sleep) as server:
        investigation_id = server.replay()
        hold.wait_reached()
        response = server.client.post(f"/api/investigations/{investigation_id}/sessions")
        assert response.status_code == 409 and response.json()["detail"] == "no_report"
        hold.released.set()
        server.wait_until(investigation_id, "reported")
        assert server.client.post("/api/investigations/inv_missing/sessions").status_code == 404
        assert server.client.get("/api/sessions/999999").status_code == 404
        missing = server.client.post("/api/sessions/999999/messages", json={"question": "Why?"})
        assert missing.status_code == 404
        session_id = open_session(server, investigation_id)
        empty = {"question": "   "}
        assert (
            server.client.post(f"/api/sessions/{session_id}/messages", json=empty).status_code
            == 422
        )


def test_recorded_mode_lists_and_enforces_the_recorded_questions(
    migrated: Engine, pg_url: str, tmp_path: Any
) -> None:
    """The question list, and 422 for a question out of order, before any model call."""
    recorded = yaml.safe_load((CASES / "s1-a" / "questions.yaml").read_text())["questions"]
    with serve(scripted(pg_url)) as server:
        investigation_id = finished(server)
    with serve(
        scripted(pg_url, llm_mode="recorded", llm_recordings_dir=tmp_path / "none")
    ) as server:
        listed = server.client.get(f"/api/investigations/{investigation_id}/questions").json()
        assert listed == {
            "recorded_only": True,
            "questions": [{"id": q["id"], "question": q["question"]} for q in recorded],
        }
        session_id = open_session(server, investigation_id)
        wrong = server.client.post(
            f"/api/sessions/{session_id}/messages", json={"question": recorded[1]["question"]}
        )
        assert wrong.status_code == 422
        assert wrong.json() == {
            "detail": "question not recorded",
            "expected": recorded[0]["question"],
        }
        # The right question reaches the model; there is no recording in this test.
        status, body = ask_raw(server, session_id, recorded[0]["question"])
    assert status == 200
    assert frames_of(body)[-1].data == {"reason": "model_error", "violations": []}


def test_scripted_mode_takes_any_question(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        investigation_id = finished(server)
        listed = server.client.get(f"/api/investigations/{investigation_id}/questions").json()
    recorded = yaml.safe_load((CASES / "s1-a" / "questions.yaml").read_text())["questions"]
    assert listed["recorded_only"] is False
    assert [q["id"] for q in listed["questions"]] == [q["id"] for q in recorded]


def test_the_sessions_of_an_investigation_are_listed_newest_first(
    migrated: Engine, pg_url: str
) -> None:
    with serve(scripted(pg_url)) as server:
        investigation_id = finished(server)
        other = finished(server, "s1-b")
        path = f"/api/investigations/{investigation_id}/sessions"
        anonymous = httpx.Client(base_url=server.url)
        assert anonymous.get(path).json() == {"sessions": []}
        first = open_session(server, investigation_id)
        ask_raw(server, first, "Why this commit?")
        ask_raw(server, first, "What else changed?")
        second = open_session(server, investigation_id)
        open_session(server, other)
        # Reading needs no login.
        listed = anonymous.get(path).json()["sessions"]
        missing = anonymous.get("/api/investigations/inv_missing/sessions")
    assert [(s["id"], s["turns"]) for s in listed] == [(second, 0), (first, 2)]
    assert all(s["created_by"] == USERNAME for s in listed)
    assert all(s["investigation_id"] == investigation_id for s in listed)
    assert missing.status_code == 404
