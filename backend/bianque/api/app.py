"""The FastAPI application.

Run it with ``uvicorn --factory bianque.api.app:create_app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bianque.api import auth, investigations
from bianque.auth.authenticator import Authenticator
from bianque.config import Settings, get_settings
from bianque.storage.db import create_engine, session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application; the database engine lives as long as the application.

    Args:
        settings: Process settings; read from the environment when omitted.
    """
    resolved = settings or get_settings()
    # One authenticator per application, holding the accounts read at startup.
    authenticator = Authenticator.from_settings(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved)
        app.state.sessions = session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Bianque", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = resolved
    app.state.authenticator = authenticator
    app.include_router(auth.router)
    app.include_router(investigations.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
