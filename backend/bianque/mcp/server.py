"""The MCP service: the tool registry offered to MCP clients over Streamable HTTP.

Tools are described from the registry and called through the tool executor, the same
ones the evidence sub-agents use (``docs/adr/0003-single-tool-registry-and-mcp.md``):
timeouts, retries, redaction and call records are those of every tool call. A client
lists only the tools its token allows; calling any other tool, or naming a namespace
outside its range, is rejected by the executor and recorded with the client as caller.

The endpoint is stateless and answers in JSON: every request stands alone, nothing is
kept between requests, and a restarted backend serves a connected client on.

MCP calls belong to no investigation: they store no evidence, and an evidence pack
backend replays the case named by ``BIANQUE_PACK_CASE``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from bianque.config import Settings
from bianque.knowledge.embedding import build_embedder
from bianque.knowledge.search import KnowledgeRetriever
from bianque.mcp.clients import STATE_KEY, BearerAuth, Client, resolve_clients
from bianque.mcp.sources import DatabaseInvestigations
from bianque.tools.backends import open_backends
from bianque.tools.executor import ToolExecutor, ToolResult
from bianque.tools.registry import ToolDefinition, ToolRegistry, registry
from bianque.tools.store import DatabaseToolCallSink

Sleep = Callable[[float], Awaitable[None]]

SERVER_NAME = "bianque"


def describe(definition: ToolDefinition) -> types.Tool:
    """The MCP declaration of a tool: the registry's description and argument schema."""
    spec = definition.spec()
    return types.Tool(
        name=spec.name,
        description=spec.description,
        input_schema=spec.parameters,
        annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )


def to_result(result: ToolResult) -> types.CallToolResult:
    """The MCP result of a tool call; a failure is a structured error, never a crash.

    The text is the executor's redacted view, the same text an evidence sub-agent sees.
    A failed or rejected call carries ``{"error": {"code", "message"}}`` both as text
    and as structured content; the code is the error category of the executor.
    """
    if result.status == "ok":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.model_view.text)]
        )
    message = result.error or result.model_view.text
    error = {"error": {"code": message.partition(":")[0], "message": message}}
    return types.CallToolResult(
        is_error=True,
        content=[types.TextContent(type="text", text=json.dumps(error, ensure_ascii=False))],
        structured_content=error,
    )


def _client(ctx: ServerRequestContext[Any, Any]) -> Client:
    # BearerAuth admits no request without a client; a missing one is a wiring defect.
    request = ctx.request
    client = request.scope.get("state", {}).get(STATE_KEY) if request is not None else None
    if not isinstance(client, Client):
        raise RuntimeError("an MCP request reached the server without an authenticated client")
    return client


def build_server(executor: ToolExecutor, tools: ToolRegistry = registry) -> Server[Any]:
    """Build the MCP server whose tools are the registry's, run by ``executor``."""

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        allowed = _client(ctx).tools
        return types.ListToolsResult(
            tools=[describe(definition) for definition in tools if definition.name in allowed]
        )

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        result = await executor.call(
            params.name, params.arguments or {}, scope=_client(ctx).scope()
        )
        return to_result(result)

    return Server(
        SERVER_NAME,
        version=version("bianque"),
        instructions=(
            "Read-only tools of Bianque, a root cause analysis agent for Kubernetes alerts: "
            "cluster state, container logs, change history, runbooks and investigation reports."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _unreachable(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError("an MCP request passed the authentication without any client")


class MCPEndpoint:
    """The ASGI application behind ``/mcp``: bearer authentication, then the MCP server.

    It is an object, not a function, so that a Starlette ``Route`` serves it as an ASGI
    application at exactly ``/mcp``; a ``Mount`` would redirect ``/mcp`` to ``/mcp/``.
    """

    def __init__(self, app: ASGIApp, clients: Sequence[Client]) -> None:
        """Serve ``app`` to the holders of the tokens of ``clients``."""
        self._app = BearerAuth(app, clients)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one request."""
        await self._app(scope, receive, send)


@asynccontextmanager
async def open_mcp(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    tool_sleep: Sleep,
    tools: ToolRegistry = registry,
) -> AsyncIterator[MCPEndpoint]:
    """Open the tool backends of MCP calls and run the MCP server while the block lasts.

    Without configured clients nothing is opened: every request is answered 401, so the
    rest of the application never depends on the backends of MCP calls. With clients, a
    backend that cannot be opened stops the start, as any misconfiguration does.

    Raises:
        ClientConfigError: A configured client's tool pattern matches no tool.
    """
    clients = resolve_clients(settings.mcp_clients, tools)
    if not clients:
        yield MCPEndpoint(_unreachable, clients)
        return
    async with AsyncExitStack() as stack:
        backends = await stack.enter_async_context(open_backends(settings))
        backends.knowledge = KnowledgeRetriever(sessions, build_embedder(settings))
        backends.investigations = DatabaseInvestigations(sessions)
        executor = ToolExecutor(
            backends, DatabaseToolCallSink(sessions), tools=tools, sleep=tool_sleep
        )
        manager = StreamableHTTPSessionManager(
            build_server(executor, tools), stateless=True, json_response=True
        )
        await stack.enter_async_context(manager.run())
        yield MCPEndpoint(manager.handle_request, clients)
