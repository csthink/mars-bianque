"""A2A clients: who may call, with which token, and the namespaces their alerts may be in.

Each client is configured with the SHA-256 of its bearer token (``BIANQUE_A2A_CLIENTS``),
the way MCP clients are. ``bianque.auth.bearer.BearerAuth`` admits a request whose token
belongs to a client and puts the client in the ASGI scope's state under ``STATE_KEY``.
``CallerContextBuilder`` then makes it the user of the SDK's call context: the user name
is the client's name, which every task records as its caller and every read of a task
is scoped to.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.routes import DefaultServerCallContextBuilder
from starlette.requests import Request

from bianque.config.settings import A2AClient

#: Where an authenticated request carries its client in the ASGI scope's state, and the
#: call context carries it in its state.
STATE_KEY = "bianque_a2a_client"


@dataclass(frozen=True)
class Client:
    """A configured A2A client."""

    name: str
    token_sha256: str
    namespaces: frozenset[str]


def resolve_clients(configured: Iterable[A2AClient]) -> tuple[Client, ...]:
    """Return the configured clients."""
    return tuple(
        Client(item.name, item.token_sha256, frozenset(item.namespaces)) for item in configured
    )


class ClientUser(User):
    """The SDK's view of an authenticated client."""

    def __init__(self, client: Client) -> None:
        """Represent ``client``."""
        self._client = client

    @property
    def is_authenticated(self) -> bool:
        """Always ``True``: only an authenticated request has a client."""
        return True

    @property
    def user_name(self) -> str:
        """The client's name."""
        return self._client.name


def _client(request: Request) -> Client:
    client = request.scope.get("state", {}).get(STATE_KEY)
    if not isinstance(client, Client):
        # BearerAuth admits no request without a client; a missing one is a wiring defect.
        raise RuntimeError("an A2A request passed the authentication without a client")
    return client


class CallerContextBuilder(DefaultServerCallContextBuilder):
    """Builds the call context of an authenticated request: its client is the user.

    The default builder's state is kept: the SDK reads the protocol version from the
    request headers it holds.
    """

    def build(self, request: Request) -> ServerCallContext:
        """Build the call context, with the client in its state."""
        context = super().build(request)
        context.state[STATE_KEY] = _client(request)
        return context

    def build_user(self, request: Request) -> User:
        """The request's client, as the SDK's user."""
        return ClientUser(_client(request))


def caller(context: ServerCallContext) -> Client:
    """The client of a call."""
    client = context.state.get(STATE_KEY)
    if not isinstance(client, Client) or context.user.user_name != client.name:
        # The endpoint admits no request without a client; a missing one is a defect.
        raise RuntimeError("an A2A call has no authenticated client")
    return client
