"""Import and retrieval on a real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from knowledge_fakes import (
    RUNBOOK_A,
    FailingEmbedder,
    HashEmbedder,
    WrongDimensionEmbedder,
    fake_vector,
    write_runbooks,
)
from sqlalchemy import Engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bianque.knowledge.chunking import chunk_markdown
from bianque.knowledge.embedding import MODEL_NAME, FixtureEmbedder, FixtureMissError
from bianque.knowledge.fixtures import text_key, write_fixture
from bianque.knowledge.ingest import ingest
from bianque.knowledge.search import VECTOR_UNAVAILABLE, KnowledgeRetriever
from bianque.storage import ids
from bianque.storage.db import session_factory
from bianque.storage.models import EMBEDDING_DIMENSION, KnowledgeChunk, KnowledgeDocument


@pytest.fixture
async def sessions(
    migrated: Engine, pg_url: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_url, poolclass=NullPool)
    yield session_factory(engine)
    await engine.dispose()


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, int]]:
    async with sessions() as session:
        rows = await session.execute(
            select(KnowledgeChunk.id, KnowledgeDocument.path, KnowledgeChunk.ordinal)
            .join(KnowledgeDocument)
            .order_by(KnowledgeDocument.path, KnowledgeChunk.ordinal)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def test_import_stores_one_row_per_chunk_with_stable_ids(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runbooks = write_runbooks(tmp_path)
    summary = await ingest(runbooks, sessions=sessions, embedder=HashEmbedder())
    assert (summary.added, summary.updated, summary.skipped, summary.deleted) == (2, 0, 0, 0)
    expected = len(chunk_markdown(RUNBOOK_A, source="a").chunks) + 1
    assert summary.chunks == expected
    rows = await _rows(sessions)
    assert len(rows) == expected
    assert rows[0][1] == "images/pull.md"
    for knowledge_id, path, ordinal in rows:
        async with sessions() as session:
            chunk = await session.get(KnowledgeChunk, knowledge_id)
        assert chunk is not None
        assert knowledge_id == ids.knowledge_id(path, chunk.heading_path, ordinal)

    again = await ingest(runbooks, sessions=sessions, embedder=HashEmbedder())
    assert (again.added, again.updated, again.skipped, again.deleted) == (0, 0, 2, 0)
    assert await _rows(sessions) == rows


async def test_a_changed_runbook_is_replaced_and_a_removed_one_deleted(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runbooks = write_runbooks(tmp_path)
    await ingest(runbooks, sessions=sessions, embedder=HashEmbedder())
    before = await _rows(sessions)
    (runbooks / "restarts.md").write_text(RUNBOOK_A + "\n## Prevention\n\nSet limits.\n")
    (runbooks / "images" / "pull.md").unlink()
    summary = await ingest(runbooks, sessions=sessions, embedder=HashEmbedder())
    assert (summary.added, summary.updated, summary.skipped, summary.deleted) == (0, 1, 0, 1)
    after = await _rows(sessions)
    assert {path for _, path, _ in after} == {"restarts.md"}
    unchanged = [row for row in before if row[1] == "restarts.md"]
    assert after[: len(unchanged)] == unchanged
    assert len(after) == len(unchanged) + 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeDocument)) == 1


async def test_import_in_fixture_mode_fails_for_a_chunk_without_a_vector(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    fixture = tmp_path / "fixture.json"
    write_fixture(
        fixture,
        {},
        model=MODEL_NAME,
        model_source="s",
        model_sha256="0" * 64,
        embedder="e",
        dimension=EMBEDDING_DIMENSION,
    )
    runbooks = write_runbooks(tmp_path / "runbooks")
    with pytest.raises(FixtureMissError):
        await ingest(runbooks, sessions=sessions, embedder=FixtureEmbedder(fixture))
    assert await _rows(sessions) == []


async def test_search_fuses_both_rankings_and_names_the_source(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runbooks = write_runbooks(tmp_path)
    await ingest(runbooks, sessions=sessions, embedder=HashEmbedder())
    exit_codes = next(
        c
        for c in chunk_markdown(RUNBOOK_A, source="a").chunks
        if c.heading_path.endswith("Exit codes")
    )
    query = "container killed exit code 137"
    # The query's vector equals the chunk's, so the vector ranking puts it first.
    embedder = HashEmbedder({query: fake_vector(exit_codes.embedding_text)})
    result = await KnowledgeRetriever(sessions, embedder).search(query)
    assert result.degraded is None
    top = result.hits[0]
    assert top.heading_path == "Pod Restarts > Diagnosis > Exit codes"
    assert top.path == "restarts.md"
    assert (top.vector_rank, top.text_rank) == (1, 1)
    assert top.score == pytest.approx(2 / 61)
    assert len(result.hits) == 5
    assert all(hit.path and hit.heading_path and hit.content for hit in result.hits)
    assert all(hit.vector_rank is not None for hit in result.hits)


async def test_retrieval_falls_back_to_full_text_when_embedding_fails(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await ingest(write_runbooks(tmp_path), sessions=sessions, embedder=HashEmbedder())
    result = await KnowledgeRetriever(sessions, FailingEmbedder()).search("image tag registry")
    assert result.degraded == VECTOR_UNAVAILABLE
    assert [hit.heading_path for hit in result.hits] == ["Image Pull Errors > Wrong tag"]
    assert result.hits[0].vector_rank is None and result.hits[0].text_rank == 1


async def test_retrieval_falls_back_to_full_text_when_the_vector_query_fails(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await ingest(write_runbooks(tmp_path), sessions=sessions, embedder=HashEmbedder())
    result = await KnowledgeRetriever(sessions, WrongDimensionEmbedder()).search("exit code")
    assert result.degraded == VECTOR_UNAVAILABLE
    assert result.hits[0].heading_path == "Pod Restarts > Diagnosis > Exit codes"


async def test_a_query_missing_from_the_fixture_degrades_instead_of_failing(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runbooks = write_runbooks(tmp_path / "runbooks")
    texts = [
        chunk.embedding_text
        for name in ("restarts.md", "images/pull.md")
        for chunk in chunk_markdown((runbooks / name).read_text(), source=name).chunks
    ]
    fixture = tmp_path / "fixture.json"
    write_fixture(
        fixture,
        {text_key(t): fake_vector(t) for t in texts},
        model=MODEL_NAME,
        model_source="s",
        model_sha256="0" * 64,
        embedder="e",
        dimension=EMBEDDING_DIMENSION,
    )
    embedder = FixtureEmbedder(fixture)
    await ingest(runbooks, sessions=sessions, embedder=embedder)
    result = await KnowledgeRetriever(sessions, embedder).search("previous container log")
    assert result.degraded == VECTOR_UNAVAILABLE
    assert result.hits[0].heading_path == "Pod Restarts > Diagnosis"


async def test_a_query_of_stop_words_only_finds_nothing_by_full_text(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await ingest(write_runbooks(tmp_path), sessions=sessions, embedder=HashEmbedder())
    result = await KnowledgeRetriever(sessions, FailingEmbedder()).search("the and of")
    assert result.hits == []
    assert result.degraded == VECTOR_UNAVAILABLE
