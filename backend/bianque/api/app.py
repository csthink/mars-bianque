"""The FastAPI application.

Run it with ``uvicorn --factory bianque.api.app:create_app``.

At startup, before any request is served, investigations left pending or running by the
previous process are marked ``interrupted``. Investigations and follow-up
answers run in background tasks of this process and are cancelled at shutdown; the
next start marks the cancelled investigations interrupted. With ``BIANQUE_REPLAY_SEED``
the replay profile's startup runs once the application serves requests.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bianque.agents.events import EventNotifier
from bianque.agents.followup import FollowupService
from bianque.agents.service import InvestigationService, interrupt_unfinished, seed_replay
from bianque.api import alerts, auth, investigations, sessions
from bianque.auth.authenticator import Authenticator
from bianque.config import Settings, get_settings
from bianque.llm import ScriptedRule
from bianque.storage.db import create_engine, session_factory

Sleep = Callable[[float], Awaitable[None]]


def create_app(
    settings: Settings | None = None,
    *,
    scripted_rules: Sequence[ScriptedRule] = (),
    tool_sleep: Sleep = asyncio.sleep,
    model_sleep: Sleep = asyncio.sleep,
) -> FastAPI:
    """Build the application; the database engine lives as long as the application.

    Args:
        settings: Process settings; read from the environment when omitted.
        scripted_rules: Rules tried before the role rules in ``scripted`` mode; tests
            inject them.
        tool_sleep: Waits between tool retries; tests skip them.
        model_sleep: Waits between model retries; tests skip them.
    """
    resolved = settings or get_settings()
    # One authenticator per application, holding the accounts read at startup.
    authenticator = Authenticator.from_settings(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved)
        factory = session_factory(engine)
        notifier = EventNotifier()
        app.state.sessions = factory
        app.state.notifier = notifier
        await interrupt_unfinished(factory, notifier)
        service = InvestigationService(
            resolved,
            factory,
            notifier,
            rules=scripted_rules,
            tool_sleep=tool_sleep,
            model_sleep=model_sleep,
        )
        followups = FollowupService(
            resolved, factory, rules=scripted_rules, model_sleep=model_sleep
        )
        app.state.investigations = service
        app.state.followups = followups
        seeding = (
            asyncio.create_task(seed_replay(resolved, factory, service))
            if resolved.replay_seed
            else None
        )
        try:
            yield
        finally:
            if seeding is not None:
                seeding.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await seeding
            await service.close()
            await followups.close()
            await engine.dispose()

    app = FastAPI(title="Bianque", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = resolved
    app.state.authenticator = authenticator
    app.include_router(auth.router)
    app.include_router(alerts.router)
    app.include_router(investigations.router)
    app.include_router(sessions.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
