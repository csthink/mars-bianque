"""MCP clients: who may call, with which token, which tools and which namespaces.

Each client is configured with the SHA-256 of its bearer token (``BIANQUE_MCP_CLIENTS``).
Its tool patterns are expanded against the registry when the application starts, so a
pattern that names no tool stops the start instead of silently granting nothing.

``bianque.auth.bearer.BearerAuth`` sits in front of the MCP endpoint: a request without
a token, or with a token of no client, is answered 401 and goes no further; there is no
caller to record. An authenticated request carries its client in the ASGI scope's state
under ``STATE_KEY``, where the MCP handlers read it.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from bianque.auth.bearer import find_client, token_sha256
from bianque.config.settings import MCPClient
from bianque.tools.executor import CallerScope
from bianque.tools.registry import ToolRegistry

__all__ = [
    "MCP_AGENT",
    "STATE_KEY",
    "Client",
    "ClientConfigError",
    "expand_tools",
    "find_client",
    "resolve_clients",
    "token_sha256",
]

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
