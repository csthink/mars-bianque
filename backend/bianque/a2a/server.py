"""The A2A endpoint: bearer authentication, then the SDK's JSON-RPC binding.

``/a2a`` answers JSON-RPC requests of the A2A protocol, streaming ones as server-sent
events. The SDK parses requests, checks the protocol version and maps errors to their
codes; ``BianqueRequestHandler`` serves the operations. The endpoint is opened in the
application's lifespan, since the handler needs the investigation service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from bianque.a2a.clients import STATE_KEY, CallerContextBuilder, Client, resolve_clients
from bianque.a2a.handler import BianqueRequestHandler
from bianque.a2a.tasks import BianqueTaskStore, TaskReader
from bianque.agents.events import EventNotifier
from bianque.agents.service import InvestigationService
from bianque.auth.bearer import BearerAuth
from bianque.config import Settings


async def _unreachable(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError("an A2A request passed the authentication without any client")


class A2AEndpoint:
    """The ASGI application behind ``/a2a``: bearer authentication, then JSON-RPC.

    It is an object, not a function, so that a Starlette ``Route`` serves it as an ASGI
    application at exactly ``/a2a``.
    """

    def __init__(self, app: ASGIApp, clients: tuple[Client, ...]) -> None:
        """Serve ``app`` to the holders of the tokens of ``clients``."""
        self._app = BearerAuth(app, clients, STATE_KEY)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one request."""
        await self._app(scope, receive, send)


@asynccontextmanager
async def open_a2a(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    notifier: EventNotifier,
    service: InvestigationService,
) -> AsyncIterator[A2AEndpoint]:
    """Serve the A2A operations while the block lasts.

    Without configured clients every request is answered 401; the Agent Card is served
    regardless (``bianque.a2a.card``).
    """
    clients = resolve_clients(settings.a2a_clients)
    if not clients:
        yield A2AEndpoint(_unreachable, clients)
        return
    store = BianqueTaskStore(sessions, TaskReader(sessions, settings.a2a_public_url))
    handler = BianqueRequestHandler(settings, sessions, notifier, service, store)
    dispatcher = JsonRpcDispatcher(handler, context_builder=CallerContextBuilder())
    yield A2AEndpoint(request_response(dispatcher.handle_requests), clients)
