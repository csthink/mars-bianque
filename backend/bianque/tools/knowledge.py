"""Auxiliary tool ``knowledge_search``: runbook retrieval for MCP clients.

The investigation and follow-up flows call the knowledge module directly; this tool
exists for MCP clients. Its result is knowledge, never evidence: the executor stores no
evidence for an auxiliary tool.
"""

from __future__ import annotations

from pydantic import Field

from bianque.tools.backends.base import ToolBackends
from bianque.tools.errors import ToolError
from bianque.tools.fields import ToolArgs
from bianque.tools.output import ToolOutput, render
from bianque.tools.registry import tool

#: Upper bound and default of the chunks one search returns.
SEARCH_MAX_LIMIT = 10
SEARCH_DEFAULT_LIMIT = 5


class KnowledgeSearchArgs(ToolArgs):
    """Arguments of ``knowledge_search``."""

    query: str = Field(min_length=1, max_length=500, description="What to look up.")
    limit: int = Field(
        default=SEARCH_DEFAULT_LIMIT,
        ge=1,
        le=SEARCH_MAX_LIMIT,
        description="How many runbook chunks to return.",
    )


@tool(
    name="knowledge_search",
    kind="auxiliary",
    read_only=True,
    timeout_s=10,
    args=KnowledgeSearchArgs,
)
async def knowledge_search(args: KnowledgeSearchArgs, backends: ToolBackends) -> ToolOutput:
    """Search the runbooks by meaning and by keywords; results are references, not evidence.

    Each result carries the knowledge ID, the runbook file and headings, its rank in the
    vector and full-text rankings, the fused score and the chunk text.
    """
    if backends.knowledge is None:
        raise ToolError("failed", "knowledge base", "no knowledge source configured")
    result = await backends.knowledge.search(args.query, args.limit)
    return ToolOutput(
        render(
            {
                "degraded": result.degraded,
                "results": [
                    {
                        "knowledge_id": hit.knowledge_id,
                        "path": hit.path,
                        "heading_path": hit.heading_path,
                        "vector_rank": hit.vector_rank,
                        "text_rank": hit.text_rank,
                        "score": round(hit.score, 6),
                        "content": hit.content,
                    }
                    for hit in result.hits
                ],
            }
        ),
        identifiers=tuple(hit.knowledge_id for hit in result.hits),
    )
