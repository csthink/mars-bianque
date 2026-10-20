"""MCP clients: who may call, with which token, which tools and which namespaces.

Each client is configured with the SHA-256 of its bearer token (``BIANQUE_MCP_CLIENTS``).
Its tool patterns are expanded against the registry when the application starts, so a
pattern that names no tool stops the start instead of silently granting nothing.

``BearerAuth`` sits in front of the MCP endpoint: a request without a token, or with a
token of no client, is answered 401 and goes no further; there is no caller to record.
An authenticated request carries its client in the ASGI scope's state, where the MCP
handlers read it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

from bianque.config.settings import MCPClient
from bianque.tools.executor import CallerScope
from bianque.tools.registry import ToolRegistry

#: The ``agent`` of every call record an MCP client causes.
MCP_AGENT = "mcp"

#: Where an authenticated request carries its client in the ASGI scope's state.
STATE_KEY = "bianque_mcp_client"


class ClientConfigError(ValueError):
    """An MCP client's configuration cannot be used."""


@dataclass(frozen=True)
class Client:
    """A configured MCP client with its tool patterns expanded to tool names."""

    name: str
    token_sha256: str
    tools: frozenset[str]
    namespaces: frozenset[str]

    def scope(self) -> CallerScope:
        """The executor's view of this client: what it may call, and its name to record."""
        return CallerScope(
            agent=MCP_AGENT, tools=self.tools, namespaces=self.namespaces, caller=self.name
        )


def expand_tools(patterns: Iterable[str], names: Sequence[str]) -> frozenset[str]:
    """Return the tool names that match any of ``patterns``.

    Raises:
        ClientConfigError: A pattern matches no tool.
    """
    matched: set[str] = set()
    for pattern in patterns:
        hits = fnmatch.filter(names, pattern)
        if not hits:
            raise ClientConfigError(f"tool pattern {pattern!r} matches no tool")
        matched.update(hits)
    return frozenset(matched)


def resolve_clients(configured: Iterable[MCPClient], tools: ToolRegistry) -> tuple[Client, ...]:
    """Expand the configured clients against the registry.

    Raises:
        ClientConfigError: A client's tool pattern matches no tool.
    """
    names = [definition.name for definition in tools]
    clients = []
    for item in configured:
        try:
            allowed = expand_tools(item.tools, names)
        except ClientConfigError as exc:
            raise ClientConfigError(f"MCP client {item.name}: {exc}") from None
        clients.append(Client(item.name, item.token_sha256, allowed, frozenset(item.namespaces)))
    return tuple(clients)


def token_sha256(token: str) -> str:
    """Return the hex SHA-256 of a token, the form clients are configured with."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_client(clients: Sequence[Client], token: str) -> Client | None:
    """Return the client holding ``token``, comparing every hash in constant time."""
    digest = token_sha256(token)
    found = None
    for client in clients:
        if hmac.compare_digest(client.token_sha256, digest):
            found = client
    return found


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return token.strip() if scheme.lower() == "bearer" and token.strip() else None
    return None


class BearerAuth:
    """Admit requests whose bearer token belongs to a client; answer 401 otherwise."""

    def __init__(self, app: ASGIApp, clients: Sequence[Client]) -> None:
        """Guard ``app`` with the tokens of ``clients``."""
        self._app = app
        self._clients = tuple(clients)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass an authenticated HTTP request on with its client in the scope's state."""
        token = _bearer(scope) if scope["type"] == "http" else None
        client = find_client(self._clients, token) if token is not None else None
        if client is None:
            body = json.dumps({"detail": "a valid bearer token is required"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})[STATE_KEY] = client
        await self._app(scope, receive, send)
