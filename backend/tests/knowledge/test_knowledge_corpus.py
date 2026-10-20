"""The committed runbooks, retrieval queries and embedding fixture fit together.

The tests that need the local embedding model fail, rather than skip, when it is not
downloaded: run ``bianque knowledge download-model`` (CI does this before the tests).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from knowledge_fakes import CASES, FIXTURE, QUERIES, RUNBOOKS
from sqlalchemy import Engine, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bianque.config import Settings
from bianque.evals.retrieval import load_queries
from bianque.gate.forbidden import find_write_commands
from bianque.knowledge.chunking import chunk_markdown
from bianque.knowledge.embedding import (
    MAX_TOKENS,
    MODEL_NAME,
    MODEL_SHA256,
    MODEL_SOURCE,
    Embedder,
    FixtureEmbedder,
    LocalEmbedder,
    ModelUnavailableError,
    find_model_file,
)
from bianque.knowledge.fixtures import coverage, fixture_texts, load_fixture
from bianque.knowledge.ingest import ingest
from bianque.knowledge.query import alert_query
from bianque.knowledge.search import KnowledgeRetriever
from bianque.storage.db import session_factory
from bianque.storage.ids import knowledge_id
from bianque.storage.models import EMBEDDING_DIMENSION, KnowledgeDocument


def _chunks() -> dict[str, tuple[str, str]]:
    """Every runbook chunk by knowledge ID: ``(path, embedding text)``."""
    chunks = {}
    for path in sorted(RUNBOOKS.glob("*.md")):
        document = chunk_markdown(path.read_text(), source=path.name)
        for ordinal, chunk in enumerate(document.chunks):
            chunks[knowledge_id(path.name, chunk.heading_path, ordinal)] = (
                path.name,
                chunk.embedding_text,
            )
    return chunks


def _query_texts() -> list[str]:
    texts = [query.query for query in load_queries(QUERIES)]
    for case in sorted(CASES.iterdir()):
        alert = json.loads((case / "pack" / "alert.json").read_text())["alerts"][0]
        texts.append(alert_query(alert["labels"], alert["annotations"]))
    return sorted(set(texts))


def _local_embedder() -> LocalEmbedder:
    model_dir = Settings().embedding_model_dir
    if find_model_file(model_dir) is None:
        pytest.fail(
            f"the local embedding model is not under {model_dir}: "
            "run `uv run bianque knowledge download-model`",
            pytrace=False,
        )
    return LocalEmbedder(model_dir)


def test_there_are_at_least_eight_runbooks_with_unique_chunk_ids() -> None:
    assert len(list(RUNBOOKS.glob("*.md"))) >= 8
    chunks = _chunks()
    assert len(chunks) == sum(
        len(chunk_markdown(p.read_text(), source=p.name).chunks) for p in RUNBOOKS.glob("*.md")
    )


def test_write_commands_appear_only_in_remediation_sections() -> None:
    for path in sorted(RUNBOOKS.glob("*.md")):
        for chunk in chunk_markdown(path.read_text(), source=path.name).chunks:
            if find_write_commands(chunk.content):
                assert " > Remediation" in chunk.heading_path, (path.name, chunk.heading_path)
                assert "confirmation" in chunk.content, (path.name, chunk.heading_path)


def test_the_fixture_covers_exactly_the_texts_it_must() -> None:
    fixture = load_fixture(FIXTURE, model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)
    queries = [query.query for query in load_queries(QUERIES)]
    missing, extra = coverage(fixture, fixture_texts(RUNBOOKS, CASES, queries))
    assert (missing, extra) == ([], []), "run make knowledge-fixtures"
    assert (fixture.model_source, fixture.model_sha256) == (MODEL_SOURCE, MODEL_SHA256)


def test_the_queries_are_annotated_with_existing_chunks() -> None:
    queries = load_queries(QUERIES)
    assert len(queries) >= 20
    known = _chunks()
    for query in queries:
        assert set(query.expected) <= known.keys(), query.id


def test_no_chunk_is_truncated_by_the_model() -> None:
    embedder = _local_embedder()
    for path, text in _chunks().values():
        assert embedder.token_count(text) <= MAX_TOKENS, (path, text[:60])
    for text in _query_texts():
        assert embedder.token_count(text) <= MAX_TOKENS, text[:60]


@pytest.fixture
async def sessions(
    migrated: Engine, pg_url: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_url, poolclass=NullPool)
    yield session_factory(engine)
    await engine.dispose()


async def _top5(
    sessions: async_sessionmaker[AsyncSession], embedder: Embedder, queries: list[str]
) -> dict[str, list[str]]:
    async with sessions() as session, session.begin():
        await session.execute(delete(KnowledgeDocument))
    await ingest(RUNBOOKS, sessions=sessions, embedder=embedder)
    retriever = KnowledgeRetriever(sessions, embedder)
    top = {}
    for query in queries:
        result = await retriever.search(query, 5)
        assert result.degraded is None, query
        top[query] = [hit.knowledge_id for hit in result.hits]
    return top


async def test_local_and_fixture_embeddings_give_the_same_top_five(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The fixture was generated on another machine; the ranking must not change."""
    queries = _query_texts()
    with_fixture = await _top5(sessions, FixtureEmbedder(FIXTURE), queries)
    with_model = await _top5(sessions, _local_embedder(), queries)
    differing = {
        q: (with_fixture[q], with_model[q]) for q in queries if with_fixture[q] != with_model[q]
    }
    assert differing == {}


def test_the_local_embedder_never_downloads(tmp_path: Path) -> None:
    embedder = LocalEmbedder(tmp_path)
    with pytest.raises(ModelUnavailableError, match="run bianque knowledge download-model"):
        _ = embedder.model_sha256()
    with pytest.raises(ModelUnavailableError):
        asyncio.run(embedder.embed(["text"]))
    assert list(tmp_path.iterdir()) == []
