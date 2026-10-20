"""The MCP service, seen by the MCP Python SDK's client over HTTP.

Each test serves the application on a local port with the evidence pack backend and
talks to ``/mcp`` with the SDK's Streamable HTTP client, as an external MCP client does.
The main cases run in both protocol modes of the client: ``legacy``, the initialize
handshake that Claude Code uses, and ``auto``, which takes the newer protocol; the SDK
handles errors on the two paths differently.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from live_server import Server, scripted, serve
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult
from sqlalchemy import Engine, text

from bianque.api.app import create_app
from bianque.llm.redacted import build_redaction_engine
from bianque.mcp import token_sha256
from bianque.tools import CallerScope, ToolExecutor, registry
from bianque.tools.backends import open_backends
from bianque.tools.pack.format import PackError
from bianque.tools.store import EvidenceRecord, ToolCallRecord

NAMESPACE = "demo-s1"

#: A token-like name no rule knows; only the entropy fallback of the redaction masks it.
OPAQUE = "Zq8" + "Lw2Xk9Vb7Nr4Tp6Ys3Md5"

#: Refuses every call record of an MCP call, as a failing database would.
REFUSE_RECORDS = (
    "CREATE OR REPLACE FUNCTION refuse_mcp_record() RETURNS trigger LANGUAGE plpgsql"
    " AS $$ BEGIN RAISE EXCEPTION 'call record refused'; END $$",
    "CREATE TRIGGER refuse_mcp_record BEFORE INSERT ON call_records"
    " FOR EACH ROW WHEN (NEW.agent = 'mcp') EXECUTE FUNCTION refuse_mcp_record()",
)

#: The client's protocol modes; Claude Code speaks the ``legacy`` handshake.
MODES = pytest.mark.parametrize("mode", ["legacy", "auto"])


def client_config(name: str, tools: list[str], namespaces: list[str]) -> tuple[str, dict[str, Any]]:
    """A fresh token and the configuration entry of a client holding it."""
    token = secrets.token_hex(16)
    entry = {
        "name": name,
        "token_sha256": token_sha256(token),
        "tools": tools,
        "namespaces": namespaces,
    }
    return token, entry


def with_mcp[T](
    server: Server, token: str, work: Callable[[Client], Awaitable[T]], mode: str = "legacy"
) -> T:
    """Connect an MCP client with ``token`` in protocol ``mode`` and run ``work`` with it."""

    async def run() -> T:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
            transport = streamable_http_client(f"{server.url}/mcp", http_client=http)
            async with Client(transport, mode=mode) as client:
                return await work(client)

    return asyncio.run(run())


def error_of(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error, result
    structured: dict[str, Any] = result.structured_content or {}
    assert json.loads(result.content[0].text) == structured  # type: ignore[union-attr]
    error: dict[str, Any] = structured["error"]
    return error


def mcp_calls(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT agent, kind, name, caller, status, retries, investigation_id, error"
                " FROM call_records WHERE agent = 'mcp' ORDER BY id"
            )
        )
        return [dict(row._mapping) for row in rows]


def evidence_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM evidence")).scalar_one())


class _Sink:
    async def save(self, call: ToolCallRecord, evidence: EvidenceRecord | None) -> None:
        pass


def direct_view(pg_url: str, name: str, arguments: dict[str, Any]) -> str:
    """The executor's redacted view of a call, made directly as an evidence sub-agent would."""
    settings = scripted(pg_url)

    async def run() -> str:
        async with open_backends(settings) as backends:
            executor = ToolExecutor(backends, _Sink(), engine=build_redaction_engine(settings))
            scope = CallerScope(agent="kubernetes", domain="kubernetes")
            return (await executor.call(name, arguments, scope=scope)).model_view.text

    return asyncio.run(run())


@MODES
def test_a_client_lists_its_tools_and_calls_one_through_the_executor(
    migrated: Engine, pg_url: str, mode: str
) -> None:
    token, entry = client_config("claude-code", ["*"], [NAMESPACE])
    with serve(scripted(pg_url, mcp_clients=[entry])) as server:

        async def work(client: Client) -> tuple[list[Any], str, CallToolResult]:
            tools = (await client.list_tools()).tools
            pods = await client.call_tool("k8s_list_pods", {"namespace": NAMESPACE})
            pod = json.loads(pods.content[0].text)["pods"][0]["name"]  # type: ignore[union-attr]
            status = await client.call_tool(
                "k8s_get_pod_status", {"namespace": NAMESPACE, "pod": pod}
            )
            return tools, pod, status

        tools, pod, status = with_mcp(server, token, work, mode)

    expected = {definition.name: definition.spec() for definition in registry}
    assert sorted(tool.name for tool in tools) == sorted(expected)
    for tool in tools:
        assert tool.input_schema == expected[tool.name].parameters
        assert tool.description == expected[tool.name].description
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
    assert not status.is_error
    # The same text an evidence sub-agent sees from the same call.
    assert status.content[0].text == direct_view(  # type: ignore[union-attr]
        pg_url, "k8s_get_pod_status", {"namespace": NAMESPACE, "pod": pod}
    )
    calls = mcp_calls(migrated)
    assert [(c["name"], c["status"], c["caller"]) for c in calls] == [
        ("k8s_list_pods", "ok", "claude-code"),
        ("k8s_get_pod_status", "ok", "claude-code"),
    ]
    assert all(c["kind"] == "tool" and c["investigation_id"] is None for c in calls)
    # MCP calls belong to no investigation and store no evidence.
    assert evidence_count(migrated) == 0


def test_requests_without_a_valid_token_are_refused(migrated: Engine, pg_url: str) -> None:
    _, entry = client_config("claude-code", ["*"], [NAMESPACE])
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    accept = {"Accept": "application/json, text/event-stream"}
    with serve(scripted(pg_url, mcp_clients=[entry])) as server:
        anonymous = httpx.Client(base_url=server.url)
        statuses = [
            anonymous.post("/mcp", json=initialize, headers=accept),
            anonymous.post(
                "/mcp", json=initialize, headers={**accept, "Authorization": "Bearer wrong"}
            ),
            anonymous.post(
                "/mcp",
                json=initialize,
                headers={**accept, "Authorization": f"Basic {entry['name']}"},
            ),
        ]
    assert [response.status_code for response in statuses] == [401, 401, 401]
    assert all(response.headers["www-authenticate"] == "Bearer" for response in statuses)
    assert mcp_calls(migrated) == []


@MODES
def test_a_client_sees_and_calls_only_its_tools(migrated: Engine, pg_url: str, mode: str) -> None:
    token, entry = client_config("pods-only", ["k8s_*"], [NAMESPACE])
    with serve(scripted(pg_url, mcp_clients=[entry])) as server:

        async def work(client: Client) -> tuple[list[str], CallToolResult]:
            names = [tool.name for tool in (await client.list_tools()).tools]
            # Arguments that would not validate: the tool is refused before they are read.
            denied = await client.call_tool("change_list_commits", {"bogus": 1})
            return names, denied

        names, denied = with_mcp(server, token, work, mode)

    assert sorted(names) == sorted(d.name for d in registry if d.name.startswith("k8s_"))
    assert error_of(denied) == {
        "code": "rejected",
        "message": "rejected: change_list_commits is not allowed for this client",
    }
    assert [(c["name"], c["status"], c["caller"]) for c in mcp_calls(migrated)] == [
        ("change_list_commits", "rejected", "pods-only")
    ]


@MODES
def test_namespaces_outside_the_range_are_refused(migrated: Engine, pg_url: str, mode: str) -> None:
    token, entry = client_config("demo-only", ["*"], [NAMESPACE])
    outside = [
        ("k8s_list_pods", {"namespace": "kube-system"}),
        ("logs_get_container_logs", {"namespace": "kube-system", "pod": "p", "container": "c"}),
        ("change_get_rollout_history", {"namespace": "kube-system", "deployment": "s1-app"}),
        ("investigation_list", {"namespace": "kube-system"}),
        (
            "investigation_get_report",
            {"namespace": "kube-system", "investigation_id": "inv_01M5CMN7F732AM2B6JP2PFKGNP"},
        ),
    ]
    with serve(scripted(pg_url, mcp_clients=[entry])) as server:

        async def work(client: Client) -> list[CallToolResult]:
            return [await client.call_tool(name, args) for name, args in outside]

        results = with_mcp(server, token, work, mode)

    for result in results:
        assert error_of(result) == {
            "code": "rejected",
            "message": "rejected: namespace kube-system is not allowed for this client",
        }
    calls = mcp_calls(migrated)
    assert [(c["name"], c["status"], c["caller"]) for c in calls] == [
        (name, "rejected", "demo-only") for name, _ in outside
    ]


@MODES
def test_a_timeout_is_a_structured_error_and_recorded_with_its_caller(
    migrated: Engine, pg_url: str, mode: str
) -> None:
    # The S1-d evidence pack injects a timeout of the log tool.
    token, entry = client_config("claude-code", ["logs_*"], [NAMESPACE])
    with serve(scripted(pg_url, mcp_clients=[entry], pack_case="s1-d")) as server:

        async def work(client: Client) -> CallToolResult:
            return await client.call_tool(
                "logs_get_container_logs",
                {"namespace": NAMESPACE, "pod": "s1-app-5468546b65-dvgq5", "container": "app"},
            )

        result = with_mcp(server, token, work, mode)

    error = error_of(result)
    assert error["code"] == "timeout"
    assert error["message"].startswith("timeout: logs_get_container_logs")
    [call] = mcp_calls(migrated)
    assert (call["name"], call["status"], call["retries"], call["caller"]) == (
        "logs_get_container_logs",
        "error",
        2,
        "claude-code",
    )


def test_investigations_are_listed_and_read_within_the_namespace(
    migrated: Engine, pg_url: str
) -> None:
    token, entry = client_config("claude-code", ["investigation_*"], [NAMESPACE, "other"])
    with serve(scripted(pg_url, mcp_clients=[entry])) as server:
        investigation_id = server.replay("s1-a")
        detail = server.wait_until(investigation_id, "reported")

        async def work(client: Client) -> list[CallToolResult]:
            report = {"investigation_id": investigation_id}
            return [
                await client.call_tool("investigation_list", {"namespace": NAMESPACE}),
                await client.call_tool("investigation_list", {"namespace": "other"}),
                await client.call_tool(
                    "investigation_get_report", {**report, "namespace": NAMESPACE}
                ),
                await client.call_tool(
                    "investigation_get_report", {**report, "namespace": "other"}
                ),
            ]

        listed, elsewhere, report, mismatched = with_mcp(server, token, work)

    rows = json.loads(listed.content[0].text)["investigations"]  # type: ignore[union-attr]
    assert [(row["investigation_id"], row["case"], row["report_status"]) for row in rows] == [
        (investigation_id, "s1-a", "published")
    ]
    assert json.loads(elsewhere.content[0].text)["investigations"] == []  # type: ignore[union-attr]
    body = json.loads(report.content[0].text)  # type: ignore[union-attr]
    assert body["report"] == detail["report"]["body"]
    assert body["report_status"] == "published"
    assert error_of(mismatched) == {
        "code": "not_found",
        "message": f"not_found: investigation {investigation_id} in other",
    }


def test_without_clients_nothing_is_opened_and_every_request_is_refused(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    # No evidence pack exists under tmp_path: the application starts all the same.
    with serve(scripted(pg_url, cases_dir=tmp_path)) as server:
        refused = httpx.post(
            f"{server.url}/mcp", json={}, headers={"Authorization": "Bearer anything"}
        )
        assert server.client.get("/healthz").status_code == 200
    assert refused.status_code == 401


def test_a_client_with_an_unusable_backend_stops_the_start(
    migrated: Engine, pg_url: str, tmp_path: Path
) -> None:
    _, entry = client_config("claude-code", ["*"], [NAMESPACE])
    app = create_app(scripted(pg_url, cases_dir=tmp_path, mcp_clients=[entry]))

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(PackError):
        asyncio.run(start())


@MODES
def test_input_the_database_cannot_store_is_rejected_and_recorded(
    migrated: Engine, pg_url: str, mode: str
) -> None:
    # PostgreSQL text cannot hold NUL. A tool name or an argument key with one must still
    # be answered with a structured error and recorded, and no database text may leak.
    token, entry = client_config("claude-code", ["*"], [NAMESPACE])
    with serve(scripted(pg_url, mcp_clients=[entry])) as server:

        async def work(client: Client) -> list[CallToolResult]:
            return [
                await client.call_tool("k8s\x00list_pods", {"namespace": NAMESPACE}),
                await client.call_tool("k8s_list_pods", {"namespace": NAMESPACE, "a\x00": 1}),
                await client.call_tool("knowledge_search", {"query": "a\x00b"}),
            ]

        unknown, invalid, query = with_mcp(server, token, work, mode)

    assert error_of(unknown)["code"] == "unknown_tool"
    assert error_of(invalid)["code"] == "invalid_arguments"
    assert error_of(query)["code"] == "invalid_arguments"
    for result in (unknown, invalid, query):
        text_ = result.content[0].text  # type: ignore[union-attr]
        assert "SQL" not in text_ and "psycopg" not in text_ and "\x00" not in text_
    calls = mcp_calls(migrated)
    assert [(c["name"], c["status"], c["caller"]) for c in calls] == [
        ("k8s\\x00list_pods", "rejected", "claude-code"),
        ("k8s_list_pods", "rejected", "claude-code"),
        ("knowledge_search", "rejected", "claude-code"),
    ]
    assert all("\x00" not in (c["error"] or "") and "SQL" not in (c["error"] or "") for c in calls)


@MODES
def test_error_messages_are_redacted_with_the_configured_engine(
    migrated: Engine, pg_url: str, mode: str
) -> None:
    token, entry = client_config("claude-code", ["*"], [NAMESPACE])
    settings = scripted(pg_url, mcp_clients=[entry], redact_internal_domains="corp.internal")
    with serve(settings) as server:

        async def work(client: Client) -> list[CallToolResult]:
            return [
                await client.call_tool(OPAQUE, {}),
                await client.call_tool("api.corp.internal", {}),
            ]

        opaque, internal = with_mcp(server, token, work, mode)

    for result, hidden in ((opaque, OPAQUE), (internal, "corp.internal")):
        error = error_of(result)
        assert error["code"] == "unknown_tool"
        assert error["message"].startswith("unknown_tool: ")
        assert hidden not in error["message"]


@MODES
def test_a_call_record_that_cannot_be_written_is_answered_without_its_cause(
    migrated: Engine, pg_url: str, mode: str
) -> None:
    token, entry = client_config("claude-code", ["*"], [NAMESPACE])
    with migrated.begin() as connection:
        for statement in REFUSE_RECORDS:
            connection.execute(text(statement))
    try:
        with serve(scripted(pg_url, mcp_clients=[entry])) as server:

            async def work(client: Client) -> CallToolResult:
                return await client.call_tool("k8s_list_pods", {"namespace": NAMESPACE})

            result = with_mcp(server, token, work, mode)
    finally:
        with migrated.begin() as connection:
            connection.execute(text("DROP TRIGGER refuse_mcp_record ON call_records"))
            connection.execute(text("DROP FUNCTION refuse_mcp_record()"))

    assert error_of(result) == {"code": "failed", "message": "failed: k8s_list_pods"}
    assert "refused" not in result.content[0].text  # type: ignore[union-attr]
    assert mcp_calls(migrated) == []
