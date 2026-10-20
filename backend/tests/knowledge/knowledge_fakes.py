"""Test doubles and paths for the knowledge tests."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from pathlib import Path

from bianque.storage.models import EMBEDDING_DIMENSION

REPO = Path(__file__).resolve().parents[3]
RUNBOOKS = REPO / "knowledge" / "runbooks"
CASES = REPO / "eval" / "cases"
QUERIES = REPO / "eval" / "retrieval" / "queries.yaml"
FIXTURE = REPO / "eval" / "embeddings" / "bge-small-en-v1.5.json"


def fake_vector(text: str) -> list[float]:
    """A deterministic unit vector derived from the text."""
    rng = random.Random(hashlib.sha256(text.encode()).digest())
    vector = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIMENSION)]
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


class HashEmbedder:
    """Embeds by hashing; a text close to a chunk is not closer than any other."""

    model = "fake"

    def __init__(self, overrides: dict[str, list[float]] | None = None) -> None:
        self.overrides = overrides or {}
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [self.overrides.get(text) or fake_vector(text) for text in texts]


class FailingEmbedder:
    """An embedder whose model is unavailable."""

    model = "fake"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")


class WrongDimensionEmbedder:
    """Returns vectors of the wrong length, so the vector query itself fails."""

    model = "fake"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


RUNBOOK_A = """# Pod Restarts

Pods that restart again and again.

## Diagnosis

Read the previous container log to see why the container exited.

### Exit codes

Exit code 137 means the container was killed, often by the kernel for memory.

## Remediation

Revert the change through Git after human confirmation.
"""

RUNBOOK_B = """# Image Pull Errors

## Wrong tag

ImagePullBackOff appears when the image tag does not exist in the registry.
"""


def write_runbooks(directory: Path, files: dict[str, str] | None = None) -> Path:
    """Write runbooks into ``directory`` and return it."""
    for name, text in (files or {"restarts.md": RUNBOOK_A, "images/pull.md": RUNBOOK_B}).items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return directory
