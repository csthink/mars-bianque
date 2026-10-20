"""Import a directory of runbooks into the knowledge tables.

Each Markdown file under the directory is one document, keyed by its path relative to
the directory. A document whose SHA-256 is unchanged is skipped; a changed one is
chunked, embedded and its chunks replaced, so importing the same file again yields the
same knowledge IDs. Documents whose files are gone are deleted with their chunks, so the
knowledge base matches the directory. Each document is written in its own transaction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.knowledge.chunking import chunk_markdown
from bianque.knowledge.embedding import Embedder
from bianque.storage.repositories import ChunkSpec, KnowledgeRepository


@dataclass(frozen=True)
class IngestSummary:
    """What an import did, counted in documents, and the chunks now stored."""

    added: int
    updated: int
    skipped: int
    deleted: int
    chunks: int


async def ingest(
    directory: Path, *, sessions: async_sessionmaker[AsyncSession], embedder: Embedder
) -> IngestSummary:
    """Import every ``*.md`` file under ``directory``.

    Raises:
        ChunkingError: A runbook does not have one H1 title.
        EmbeddingError: A chunk cannot be embedded; in ``fixture`` mode, the fixture
            has no vector for it.
    """
    files = {path.relative_to(directory).as_posix(): path for path in directory.rglob("*.md")}
    async with sessions() as session:
        documents = await KnowledgeRepository(session).list_documents()
    stored = {doc.path: doc.sha256 for doc in documents}
    added = updated = skipped = 0
    for relative in sorted(files):
        raw = files[relative].read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if stored.get(relative) == digest:
            skipped += 1
            continue
        document = chunk_markdown(raw.decode(), source=relative)
        vectors = await embedder.embed([chunk.embedding_text for chunk in document.chunks])
        async with sessions() as session, session.begin():
            repository = KnowledgeRepository(session)
            row = await repository.upsert_document(
                path=relative, title=document.title, sha256=digest
            )
            await repository.replace_chunks(
                row,
                [
                    ChunkSpec(chunk.heading_path, chunk.content, vector)
                    for chunk, vector in zip(document.chunks, vectors, strict=True)
                ],
            )
        if relative in stored:
            updated += 1
        else:
            added += 1
    deleted = 0
    async with sessions() as session, session.begin():
        repository = KnowledgeRepository(session)
        for doc in await repository.list_documents():
            if doc.path not in files:
                await repository.delete_document(doc)
                deleted += 1
        chunks = await repository.count_chunks()
    return IngestSummary(added, updated, skipped, deleted, chunks)
