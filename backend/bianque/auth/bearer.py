"""Bearer tokens of service clients: their hashes, and the check in front of an endpoint.

A service client, such as an MCP client or an A2A client, is configured with the
SHA-256 of its bearer token, never with the token. ``BearerAuth`` sits in front of an
endpoint: a request without a token, or with a token of no client, is answered 401 with
``WWW-Authenticate: Bearer`` and goes no further; there is no caller to record. An
authenticated request carries its client in the ASGI scope's state, under a key the
endpoint chooses, where the endpoint's handlers read it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from typing import Protocol

from starlette.types import ASGIApp, Receive, Scope, Send


class TokenHolder(Protocol):
    """A configured client: what ``BearerAuth`` needs of it."""

    @property
    def token_sha256(self) -> str:
        """The hex SHA-256 of the client's token."""
        ...


def token_sha256(token: str) -> str:
    """Return the hex SHA-256 of a token, the form clients are configured with."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_client[C: TokenHolder](clients: Sequence[C], token: str) -> C | None:
    """Return the client holding ``token``, comparing every hash in constant time."""
    digest = token_sha256(token)
    found = None
    for client in clients:
        if hmac.compare_digest(client.token_sha256, digest):
            found = client
    return found


def bearer_token(scope: Scope) -> str | None:
    """Return the token of the request's ``Authorization: Bearer`` header, if any."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return token.strip() if scheme.lower() == "bearer" and token.strip() else None
    return None


async def refuse(send: Send) -> None:
    """Answer 401 and name the authentication the endpoint needs."""
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


class BearerAuth[C: TokenHolder]:
    """Admit requests whose bearer token belongs to a client; answer 401 otherwise."""

    def __init__(self, app: ASGIApp, clients: Sequence[C], state_key: str) -> None:
        """Guard ``app`` with the tokens of ``clients``.

        Args:
            app: The endpoint.
            clients: The configured clients; none means every request is refused.
            state_key: Where an admitted request carries its client in the scope's state.
        """
        self._app = app
        self._clients = tuple(clients)
        self._state_key = state_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass an authenticated HTTP request on with its client in the scope's state."""
        token = bearer_token(scope) if scope["type"] == "http" else None
        client = find_client(self._clients, token) if token is not None else None
        if client is None:
            await refuse(send)
            return
        scope.setdefault("state", {})[self._state_key] = client
        await self._app(scope, receive, send)
