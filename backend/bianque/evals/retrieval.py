"""Retrieval evaluation: hit@k and MRR of the fused retrieval.

Each query of ``eval/retrieval/queries.yaml`` is annotated with the knowledge IDs it
should find. The evaluation runs the fused retrieval for every query, keeps the top five
and reports, over all queries, hit@1, hit@3 and hit@5 (the share of queries with an
annotated chunk among the first k) and the mean reciprocal rank of the first annotated
chunk (0 when none is in the top five). Only the fused retrieval is evaluated.

A degraded retrieval fails the evaluation instead of lowering its numbers: in
``fixture`` mode it means the fixture lacks a query.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from bianque.knowledge.search import KnowledgeSearch

#: Version of the query file format; a file with another version fails to load.
SCHEMA_VERSION = 1

#: The depth of the evaluated ranking and the cut-offs reported.
TOP = 5
CUTOFFS = (1, 3, 5)


class RetrievalEvalError(RuntimeError):
    """The evaluation cannot produce trustworthy numbers; the message says why."""


@dataclass(frozen=True)
class RetrievalQuery:
    """One annotated query."""

    id: str
    query: str
    expected: tuple[str, ...]


@dataclass(frozen=True)
class QueryOutcome:
    """What the retrieval returned for one query."""

    id: str
    top: list[str]
    first_hit_rank: int | None


def load_queries(path: Path) -> list[RetrievalQuery]:
    """Load the annotated queries.

    Raises:
        RetrievalEvalError: The file is missing or malformed, its schema version
            differs, or query IDs repeat; the message names the file.
    """
    try:
        data: Any = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise RetrievalEvalError(f"{path}: cannot read the queries: {exc}") from exc
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version != SCHEMA_VERSION:
        raise RetrievalEvalError(
            f"{path}: schema_version is {version!r}, expected {SCHEMA_VERSION!r}"
        )
    try:
        queries = [
            RetrievalQuery(str(item["id"]), str(item["query"]), tuple(item["expected"]))
            for item in data["queries"]
        ]
    except (KeyError, TypeError) as exc:
        raise RetrievalEvalError(f"{path}: malformed query: {exc}") from exc
    ids = [query.id for query in queries]
    if len(set(ids)) != len(ids):
        raise RetrievalEvalError(f"{path}: query IDs repeat")
    if any(not query.expected for query in queries):
        raise RetrievalEvalError(f"{path}: every query needs at least one expected ID")
    return queries


def check_annotations(
    queries: Sequence[RetrievalQuery], known: Collection[str], path: Path
) -> None:
    """Fail when an annotation names a knowledge ID that is not imported.

    Raises:
        RetrievalEvalError: Names the stale IDs.
    """
    stale = sorted({kid for query in queries for kid in query.expected if kid not in known})
    if stale:
        raise RetrievalEvalError(f"{path}: annotated IDs not in the knowledge base: {stale}")


def metrics(outcomes: Sequence[QueryOutcome]) -> dict[str, float]:
    """Return hit@k for each cut-off and the MRR, over all outcomes."""
    count = len(outcomes)
    if count == 0:
        raise RetrievalEvalError("no queries to evaluate")
    result = {
        f"hit_at_{k}": sum(1 for o in outcomes if o.first_hit_rank and o.first_hit_rank <= k)
        / count
        for k in CUTOFFS
    }
    result["mrr"] = sum(1 / o.first_hit_rank for o in outcomes if o.first_hit_rank) / count
    return result


async def evaluate(
    queries: Sequence[RetrievalQuery], retriever: KnowledgeSearch
) -> list[QueryOutcome]:
    """Run the fused retrieval for every query.

    Raises:
        RetrievalEvalError: A retrieval was degraded.
    """
    outcomes = []
    for query in queries:
        result = await retriever.search(query.query, TOP)
        if result.degraded:
            raise RetrievalEvalError(
                f"query {query.id}: retrieval degraded ({result.degraded}); "
                "in fixture mode, regenerate the fixture with make knowledge-fixtures"
            )
        top = [hit.knowledge_id for hit in result.hits]
        rank = next((i for i, kid in enumerate(top, start=1) if kid in query.expected), None)
        outcomes.append(QueryOutcome(query.id, top, rank))
    return outcomes


def write_report(
    out: Path, outcomes: Sequence[QueryOutcome], *, context: dict[str, str]
) -> dict[str, Any]:
    """Write ``retrieval.json`` and ``retrieval.md`` into ``out`` and return the JSON.

    Args:
        out: The output directory.
        outcomes: The per-query outcomes.
        context: How the numbers were produced: embedding mode, model, files and hashes.
    """
    values = metrics(outcomes)
    report = {
        "schema_version": SCHEMA_VERSION,
        **context,
        "top": TOP,
        "queries": len(outcomes),
        "metrics": {name: round(value, 4) for name, value in values.items()},
        "outcomes": [asdict(outcome) for outcome in outcomes],
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "retrieval.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Retrieval evaluation",
        "",
        *(f"- {key}: {value}" for key, value in context.items()),
        f"- queries: {len(outcomes)}, ranking depth: {TOP}",
        "",
        "| metric | value |",
        "| --- | --- |",
        *(f"| {name} | {value:.4f} |" for name, value in values.items()),
        "",
        "| query | first hit rank |",
        "| --- | --- |",
        *(f"| {o.id} | {o.first_hit_rank or '-'} |" for o in outcomes),
        "",
    ]
    (out / "retrieval.md").write_text("\n".join(lines))
    return report
