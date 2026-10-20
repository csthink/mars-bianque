"""A backend killed during an investigation; the next start marks it interrupted."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from demo_login import PASSWORD, USERNAME
from live_server import CASES, TIMEOUT, scripted, serve
from sqlalchemy import Engine

LAUNCHER = Path(__file__).with_name("held_server.py")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _webhook() -> dict[str, object]:
    body: dict[str, object] = json.loads((CASES / "s1-a" / "pack" / "alert.json").read_text())
    return body


def _wait(predicate: object, what: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, what
        time.sleep(0.1)


def test_a_killed_backend_leaves_an_interrupted_investigation(
    migrated: Engine, pg_url: str
) -> None:
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER), pg_url, str(port)],
        cwd=LAUNCHER.parent,
        env={
            **os.environ,
            "PYTHONPATH": str(LAUNCHER.parents[2]),
            "HELD_SERVER_PASSWORD": PASSWORD,
        },
    )
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=TIMEOUT)

        def healthy() -> bool:
            try:
                return client.get("/healthz").status_code == 200
            except httpx.TransportError:
                return False

        _wait(healthy, "the held backend did not start")
        login = {"username": USERNAME, "password": PASSWORD}
        assert client.post("/api/auth/login", json=login).status_code == 200
        delivered = client.post("/api/alerts/alertmanager", json=_webhook()).json()
        investigation_id = delivered["alerts"][0]["investigation_id"]

        def collecting() -> bool:
            detail = client.get(f"/api/investigations/{investigation_id}").json()
            steps = [s for plan in detail["plans"] for s in plan["steps"]]
            return any(s["status"] == "running" for s in steps)

        _wait(collecting, "the investigation did not reach its sub-agents")
        before = client.get(f"/api/investigations/{investigation_id}").json()
        assert before["investigation"]["status"] == "running"
    finally:
        process.send_signal(signal.SIGKILL)
        process.wait(TIMEOUT)

    with serve(scripted(pg_url)) as server:
        after = server.investigation(investigation_id)
        events = server.client.get(f"/api/investigations/{investigation_id}/events").text
        redelivered = server.client.post("/api/alerts/alertmanager", json=_webhook()).json()
        new_id = redelivered["alerts"][0]["investigation_id"]
        assert new_id and new_id != investigation_id
        assert redelivered["alerts"][0]["deduplicated"] is False
        server.wait_until(new_id, "reported")

    assert after["investigation"]["status"] == "interrupted"
    assert after["investigation"]["finished_at"] is not None
    assert "event: investigation_interrupted" in events
    assert '"status": "interrupted"' in events
    # What was stored before the kill is still there.
    assert [p["id"] for p in after["plans"]] == [p["id"] for p in before["plans"]]
    assert {e["id"] for e in after["evidence"]} >= {e["id"] for e in before["evidence"]}
    assert any(e["source"] == "alertmanager" for e in after["evidence"])
    steps = [s for plan in after["plans"] for s in plan["steps"]]
    assert steps and all(s["status"] in ("done", "failed") for s in steps)
    assert any(s["status"] == "failed" for s in steps)
