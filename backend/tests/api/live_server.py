"""The application served by a real uvicorn server on a local port, for streaming tests.

The test client of Starlette returns a response only once its body is complete, so it
cannot show that events arrive while an investigation runs. These tests talk HTTP to a
server in a thread instead; the investigations run in the server's event loop as they
do in production. Plain ``http``: the session cookie is configured without ``Secure``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from demo_login import PASSWORD, USERNAME, settings

from bianque.api.app import Sleep, create_app
from bianque.config import Settings
from bianque.llm import ScriptedReply, ScriptedRule
from bianque.llm.errors import RetryableProviderError
from bianque.llm.request import ModelRequest

REPO = Path(__file__).resolve().parents[3]
CASES = REPO / "eval" / "cases"
RUNBOOKS = REPO / "knowledge" / "runbooks"
EMBEDDING_FIXTURE = REPO / "eval" / "embeddings" / "bge-small-en-v1.5.json"

#: Seconds a test waits for an investigation or a server before it fails.
TIMEOUT = 60.0


def scripted(pg_url: str, **overrides: object) -> Settings:
    """Settings of the replay profile in ``scripted`` mode on the test database."""
    values: dict[str, object] = {
        "llm_mode": "scripted",
        "tool_backend": "pack",
        "cases_dir": CASES,
        "knowledge_dir": RUNBOOKS,
        "embedding_mode": "fixture",
        "embedding_fixture": EMBEDDING_FIXTURE,
        "session_cookie_secure": False,
    }
    values.update(overrides)
    return settings(pg_url, **values)


async def no_wait(_: float) -> None:
    """A sleep that returns at once, for retries in tests."""


@dataclass
class Server:
    """A running server and a client of it, logged in with the demo account."""

    url: str
    client: httpx.Client

    def login(self) -> None:
        """Log in with the demo account; the client keeps the session cookie."""
        response = self.client.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text

    def investigation(self, investigation_id: str) -> dict[str, Any]:
        """The detail of an investigation."""
        response = self.client.get(f"/api/investigations/{investigation_id}")
        assert response.status_code == 200, response.text
        detail: dict[str, Any] = response.json()
        return detail

    def wait_until(self, investigation_id: str, *statuses: str) -> dict[str, Any]:
        """Poll the detail until the investigation has one of ``statuses``."""
        deadline = time.monotonic() + TIMEOUT
        while True:
            detail = self.investigation(investigation_id)
            if detail["investigation"]["status"] in statuses:
                return detail
            assert time.monotonic() < deadline, detail["investigation"]
            time.sleep(0.05)

    def replay(self, case: str = "s1-a") -> str:
        """Start a replay of a case and return the investigation ID."""
        response = self.client.post("/api/investigations", json={"case_id": case})
        assert response.status_code == 202, response.text
        investigation_id: str = response.json()["investigation_id"]
        return investigation_id


@contextmanager
def serve(
    app_settings: Settings,
    *,
    rules: Sequence[ScriptedRule] = (),
    model_sleep: Sleep = no_wait,
    tool_sleep: Sleep = no_wait,
) -> Iterator[Server]:
    """Serve the application on a free local port until the block ends."""
    app = create_app(
        app_settings, scripted_rules=rules, model_sleep=model_sleep, tool_sleep=tool_sleep
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + TIMEOUT
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=url, timeout=TIMEOUT) as client:
            running = Server(url, client)
            running.login()
            yield running
    finally:
        server.should_exit = True
        thread.join(TIMEOUT)


@dataclass(frozen=True)
class Frame:
    """One server-sent event."""

    event: str
    data: Mapping[str, Any]
    id: str | None
    received_at: float


def read_frames(lines: Iterator[str]) -> Iterator[Frame]:
    """Parse server-sent events from the lines of a response; comments are skipped."""
    event, data, event_id = "message", "", None
    for line in lines:
        if line == "":
            if data:
                yield Frame(event, json.loads(data), event_id, time.monotonic())
            event, data, event_id = "message", "", None
        elif line.startswith(":"):
            continue
        elif line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data += line[len("data: ") :]
        elif line.startswith("id: "):
            event_id = line[len("id: ") :]


class Hold:
    """Keeps the model calls of some agents waiting until the test releases them.

    The rule fails the first call of a held agent as a timeout; the gateway then waits
    with ``sleep`` before its retry, and ``sleep`` returns only once released. The
    investigation stays running at a known point meanwhile.
    """

    def __init__(self, *agents: str) -> None:
        """Hold the calls of ``agents``."""
        self.agents = set(agents)
        self.reached = threading.Event()
        self.released = threading.Event()

    def rule(self, request: ModelRequest) -> ScriptedReply | None:
        """Fail a held call until released; leave every other call to the role rules."""
        if request.agent in self.agents and not self.released.is_set():
            self.reached.set()
            raise RetryableProviderError("timeout", "held by the test")
        return None

    async def sleep(self, _: float) -> None:
        """Wait until released."""
        while not self.released.is_set():
            await asyncio.sleep(0.01)

    def wait_reached(self) -> None:
        """Block until a held call was made."""
        assert self.reached.wait(TIMEOUT), "no held call was made"
