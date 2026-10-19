from collections.abc import Iterator
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from bianque.storage.models import Base

OWN_TABLES = set(Base.metadata.tables)


def _public_objects(engine: Engine) -> dict[str, set[str]]:
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        functions = set(
            connection.scalars(
                text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.prokind = 'f' "
                    "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid "
                    "AND d.deptype = 'e')"
                )
            )
        )
        triggers = set(
            connection.scalars(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
        )
        schemas = set(connection.scalars(text("SELECT nspname FROM pg_namespace")))
    return {"tables": tables, "functions": functions, "triggers": triggers, "schemas": schemas}


@pytest.fixture
def empty(migrator: Any) -> Iterator[Any]:
    migrator.downgrade()
    yield migrator
    migrator.downgrade()


def test_upgrade_downgrade_round_trip(empty: Any) -> None:
    engine = empty.engine
    before = _public_objects(engine)
    assert not before["tables"] & OWN_TABLES

    empty.upgrade()
    migrated = _public_objects(engine)
    assert OWN_TABLES <= migrated["tables"]
    assert "confirmations_append_only" in migrated["functions"]
    assert "confirmations_append_only" in migrated["triggers"]
    assert "langgraph" in migrated["schemas"]

    empty.downgrade()
    after = _public_objects(engine)
    assert after["tables"] - {"alembic_version"} == before["tables"] - {"alembic_version"}
    assert after["functions"] == before["functions"]
    assert after["triggers"] == before["triggers"]
    assert "langgraph" not in after["schemas"]

    empty.upgrade()
    assert OWN_TABLES <= _public_objects(engine)["tables"]


def test_models_match_migrated_schema(migrated: Engine) -> None:
    with migrated.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "target_metadata": Base.metadata}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == []


def test_vector_extension_and_gin_index_exist(migrated: Engine) -> None:
    with migrated.connect() as connection:
        assert connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
        index = connection.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_knowledge_chunks_tsv'")
        )
    assert index is not None and "USING gin" in index
