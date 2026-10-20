"""A backend process whose evidence sub-agents never get an answer, for the kill test.

``python tests/api/held_server.py <database URL> <port>`` serves the replay profile in
``scripted`` mode; the demo account's password comes from ``HELD_SERVER_PASSWORD``.
The first model call of each evidence sub-agent fails as a timeout and the retry waits
for an hour, so an investigation stays running with its plan and its alert evidence
stored until the test kills the process.
"""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn
from demo_login import USERNAME
from live_server import scripted

from bianque.api.app import create_app
from bianque.auth.passwords import hash_password
from bianque.llm import ScriptedReply
from bianque.llm.errors import RetryableProviderError
from bianque.llm.request import ModelRequest


def held(request: ModelRequest) -> ScriptedReply | None:
    if request.agent in ("kubernetes", "logs"):
        raise RetryableProviderError("timeout", "held until the process is killed")
    return None


async def an_hour(_: float) -> None:
    await asyncio.sleep(3600)


def main() -> None:
    database_url, port = sys.argv[1], int(sys.argv[2])
    password_hash = hash_password(os.environ["HELD_SERVER_PASSWORD"], rounds=4)
    app_settings = scripted(database_url, demo_users={USERNAME: password_hash})
    app = create_app(app_settings, scripted_rules=[held], model_sleep=an_hour)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
