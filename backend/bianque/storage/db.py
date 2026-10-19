"""Database engine and session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

from bianque.config import Settings
from bianque.storage.errors import DatabaseNotConfiguredError


def database_url(settings: Settings) -> str:
    """Return the configured database URL.

    Args:
        settings: Process settings.

    Returns:
        The value of ``BIANQUE_DATABASE_URL``, a ``postgresql+psycopg://`` URL.

    Raises:
        DatabaseNotConfiguredError: The setting is empty.
    """
    if not settings.database_url:
        raise DatabaseNotConfiguredError("BIANQUE_DATABASE_URL is not set")
    return settings.database_url


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the asynchronous engine for the configured database.

    Args:
        settings: Process settings.

    Returns:
        An engine using the psycopg driver.
    """
    return _create_async_engine(database_url(settings), pool_pre_ping=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to ``engine``.

    Objects stay usable after commit (``expire_on_commit=False``), so a repository can
    return a row that the caller reads after committing.

    Args:
        engine: The engine to bind.

    Returns:
        The session factory.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
