"""Shared fixtures: a real PostgreSQL for storage and API tests.

Database constraints, triggers and generated columns can only be proven on a real
PostgreSQL, so these tests never skip:

- When ``BIANQUE_TEST_DATABASE_URL`` is set (CI uses a service container), that
  database is used.
- Otherwise a temporary ``pgvector/pgvector:pg16`` container is started with Docker on
  a random local port and removed after the session.
- When neither is available the tests fail with an explanation.

Every test starts from a freshly migrated schema and downgrades to an empty one when it
ends, so tests do not depend on each other.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bianque.storage.db import session_factory

BACKEND_DIR = Path(__file__).resolve().parents[1]
IMAGE = "pgvector/pgvector:pg16"
URL_ENV = "BIANQUE_TEST_DATABASE_URL"
STARTUP_TIMEOUT_SECONDS = 60

#: Tests that read the settings from the environment use the repository's skills; the
#: default ``.agents/skills`` is relative to the repository root, not to ``backend``.
os.environ.setdefault("BIANQUE_SKILLS_DIR", str(BACKEND_DIR.parent / ".agents" / "skills"))


def _wait_until_ready(url: str) -> None:
    conninfo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while True:
        try:
            with psycopg.connect(conninfo, connect_timeout=2):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """URL of a PostgreSQL with pgvector, from the environment or a Docker container."""
    url = os.environ.get(URL_ENV)
    if url:
        _wait_until_ready(url)
        yield url
        return
    if shutil.which("docker") is None:
        pytest.fail(
            f"these tests need PostgreSQL: set {URL_ENV} or install Docker so that a "
            f"temporary {IMAGE} container can be started. They fail instead of skipping "
            "because database constraints are only proven on a real database.",
            pytrace=False,
        )
    name = f"bianque-test-{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    subprocess.run(
        [
            "docker", "run", "--detach", "--rm", "--name", name,
            "--env", f"POSTGRES_PASSWORD={password}",
            "--publish", "127.0.0.1::5432",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    try:
        port = (
            subprocess.run(
                ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
            )
            .stdout.strip()
            .rsplit(":", 1)[-1]
        )
        url = f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres"
        _wait_until_ready(url)
        yield url
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)


@pytest.fixture
def sync_engine(pg_url: str) -> Iterator[Engine]:
    """A synchronous engine for migrations and catalog checks."""
    engine = create_engine(pg_url, poolclass=NullPool)
    yield engine
    engine.dispose()


class Migrator:
    """Runs Alembic migrations on one engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._config = Config(str(BACKEND_DIR / "alembic.ini"))

    def upgrade(self, revision: str = "head") -> None:
        with self.engine.begin() as connection:
            self._config.attributes["connection"] = connection
            command.upgrade(self._config, revision)

    def downgrade(self, revision: str = "base") -> None:
        with self.engine.begin() as connection:
            self._config.attributes["connection"] = connection
            command.downgrade(self._config, revision)


@pytest.fixture
def migrator(sync_engine: Engine) -> Migrator:
    """Migration helper bound to the test database."""
    return Migrator(sync_engine)


@pytest.fixture
def migrated(migrator: Migrator) -> Iterator[Engine]:
    """A database migrated to the latest revision, emptied again after the test."""
    migrator.upgrade()
    yield migrator.engine
    migrator.downgrade()


@pytest.fixture
async def session(migrated: Engine, pg_url: str) -> AsyncIterator[AsyncSession]:
    """An asynchronous session on the migrated database."""
    engine = create_async_engine(pg_url, poolclass=NullPool)
    async with session_factory(engine)() as db_session:
        yield db_session
    await engine.dispose()
