"""The MCP server's turning of tool outcomes into results, failures included."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from bianque.config.settings import MCPClient
from bianque.mcp.clients import resolve_clients, token_sha256
from bianque.mcp.server import run_tool
from bianque.redaction import build_engine
from bianque.tools import ToolExecutor, registry
from bianque.tools.backends.base import ToolBackends
from bianque.tools.store import EvidenceRecord, ToolCallRecord

#: What a failing database driver might say: a statement and its parameters.
DATABASE_TEXT = "INSERT INTO call_records (name, error) VALUES (%(name)s, %(error)s)"


class _BrokenSink:
    async def save(self, call: ToolCallRecord, evidence: EvidenceRecord | None) -> None:
        raise RuntimeError(DATABASE_TEXT)


def _executor() -> ToolExecutor:
    backends = ToolBackends(
        kind="pack",
        kubernetes=None,  # type: ignore[arg-type]  # never reached: the call is unknown
        git=None,  # type: ignore[arg-type]
        now=lambda: datetime.now(UTC),
    )
    return ToolExecutor(backends, _BrokenSink(), tools=registry, engine=build_engine())


async def test_an_exception_is_logged_and_answered_without_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    [client] = resolve_clients(
        [
            MCPClient(
                name="claude-code",
                token_sha256=token_sha256("t"),
                tools=("*",),
                namespaces=("demo-s1",),
            )
        ],
        registry,
    )
    with caplog.at_level(logging.ERROR, logger="bianque.mcp.server"):
        result = await run_tool(_executor(), client, "no_such_tool", {})
    assert result.is_error
    error = {"error": {"code": "failed", "message": "failed: no_such_tool"}}
    assert result.structured_content == error
    assert json.loads(result.content[0].text) == error  # type: ignore[union-attr]
    assert DATABASE_TEXT in caplog.text  # the cause stays in the server's log
