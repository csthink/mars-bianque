"""Hybrid retrieval over the runbook chunks.

Two rankings are fused with reciprocal rank fusion (RRF): the exact cosine distance of
the chunk embeddings to the query embedding, and the full-text rank of the chunks that
match any word of the query. Each ranking keeps its top ``CANDIDATES``; a chunk's RRF
score is the sum of ``1 / (RRF_K + rank)`` over the rankings it appears in, and the top
``limit`` by score are returned, ties by knowledge ID. Every ordering ends with the
knowledge ID, so the result is the same on every machine.

When the query cannot be embedded or the vector query fails, retrieval falls back to
the full-text ranking alone and says so (``degraded``); a failing full-text query is
not caught.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.knowledge.embedding import Embedder
from bianque.storage.repositories import KnowledgeRepository

logger = logging.getLogger(__name__)

#: Candidates taken from each ranking before fusion.
CANDIDATES = 20

#: The RRF constant.
RRF_K = 60

#: Chunks returned by default.
DEFAULT_LIMIT = 5

#: Why retrieval used the full-text ranking alone.
Degradation = Literal["vector_unavailable"]

#: The ``degraded`` value when only the full-text ranking was used.
VECTOR_UNAVAILABLE: Degradation = "vector_unavailable"


@dataclass(frozen=True)
class Fused:
    """One chunk after fusion: its ranks (from 1) in each ranking and its RRF score."""

    knowledge_id: str
    vector_rank: int | None
    text_rank: int | None
    score: float


def rrf(
    vector_ids: Sequence[str],
    text_ids: Sequence[str],
    *,
    k: int = RRF_K,
    limit: int = DEFAULT_LIMIT,
) -> list[Fused]:
    """Fuse two rankings with reciprocal rank fusion.

    Args:
        vector_ids: Chunk IDs, best first, by vector distance.
        text_ids: Chunk IDs, best first, by full-text rank.
        k: The RRF constant.
        limit: How many fused results to return.

    Returns:
        The top ``limit`` chunks by score, ties broken by knowledge ID.
    """
    vector_rank = {item: rank for rank, item in enumerate(vector_ids, start=1)}
    text_rank = {item: rank for rank, item in enumerate(text_ids, start=1)}
    fused = [
        Fused(
            knowledge_id=item,
            vector_rank=vector_rank.get(item),
            text_rank=text_rank.get(item),
            score=sum(1 / (k + rank[item]) for rank in (vector_rank, text_rank) if item in rank),
        )
        for item in vector_rank.keys() | text_rank.keys()
    ]
    fused.sort(key=lambda hit: (-hit.score, hit.knowledge_id))
    return fused[:limit]


@dataclass(frozen=True)
class KnowledgeHit:
    """A retrieved chunk with its source and ranks.

    Attributes:
        knowledge_id: The ``kn_`` ID.
        path: The runbook file, relative to the knowledge root.
        heading_path: The headings from the title down to the chunk.
        content: The chunk's text.
        vector_rank: Rank in the vector ranking, from 1; ``None`` when not in it.
        text_rank: Rank in the full-text ranking, from 1; ``None`` when not in it.
        score: The RRF score.
    """

    knowledge_id: str
    path: str
    heading_path: str
    content: str
    vector_rank: int | None
    text_rank: int | None
    score: float


@dataclass(frozen=True)
class SearchResult:
    """The chunks found for a query, best first."""

    hits: list[KnowledgeHit]
    degraded: Degradation | None = None


class KnowledgeSearch(Protocol):
    """Anything that searches the runbooks like ``KnowledgeRetriever``."""

    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> SearchResult:
        """Return the best ``limit`` chunks for ``query``."""
        ...


class KnowledgeRetriever:
    """Searches the runbook chunks; the knowledge module's one search entry point."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        """Search the chunks in the database of ``sessions``, embedding with ``embedder``."""
        self._sessions = sessions
        self._embedder = embedder

    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> SearchResult:
        """Return the best ``limit`` chunks for ``query``."""
        embedding: list[float] | None = None
        try:
            embedding = (await self._embedder.embed([query]))[0]
        except Exception:
            logger.warning("query embedding failed; using full-text retrieval only", exc_info=True)
        async with self._sessions() as session, session.begin():
            repository = KnowledgeRepository(session)
            text_ids = await repository.text_ranking(query, CANDIDATES)
            vector_ids: list[str] | None = None
            if embedding is not None:
                try:
                    async with session.begin_nested():
                        vector_ids = await repository.vector_ranking(embedding, CANDIDATES)
                except Exception:
                    logger.warning(
                        "vector query failed; using full-text retrieval only", exc_info=True
                    )
            fused = rrf(vector_ids or [], text_ids, limit=limit)
            chunks = await repository.get_chunks([hit.knowledge_id for hit in fused])
        hits = [
            KnowledgeHit(
                knowledge_id=chunk.id,
                path=document.path,
                heading_path=chunk.heading_path,
                content=chunk.content,
                vector_rank=hit.vector_rank,
                text_rank=hit.text_rank,
                score=hit.score,
            )
            for hit, (chunk, document) in zip(fused, chunks, strict=True)
        ]
        return SearchResult(hits, None if vector_ids is not None else VECTOR_UNAVAILABLE)
