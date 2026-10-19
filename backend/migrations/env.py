"""Alembic environment.

Migrations run synchronously over the psycopg driver. The connection comes from
``config.attributes["connection"]`` when a caller (such as a test) provides one, and
otherwise from ``BIANQUE_DATABASE_URL``.

Only the default schema is managed here. The ``langgraph`` schema holds the
checkpoint tables that the checkpoint library creates itself, so autogenerate and
comparisons ignore it.
"""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from bianque.config import get_settings
from bianque.storage.db import database_url
from bianque.storage.models import Base

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Keep only objects of the default schema."""
    if type_ == "schema":
        return name is None
    return True


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(database_url(get_settings()), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported; run against a database")
run_migrations_online()
