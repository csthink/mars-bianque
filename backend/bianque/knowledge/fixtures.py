"""The embedding fixture: precomputed vectors for every text the evaluation embeds.

Recording happens on a Mac (ARM) and CI runs on x86; the same model may produce slightly
different floats on the two, and a changed ranking changes the prompt, so recorded model
responses would no longer match. The fixture fixes the vectors instead: it
is generated once with the local model and read in ``fixture`` mode.

Vectors are stored as the base64 of their little-endian float32 bytes, so a vector reads
back bit for bit as it was written; decimal floats in JSON would not guarantee that.
Keys are the SHA-256 of the embedded text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bianque.knowledge.chunking import chunk_markdown
from bianque.knowledge.query import alert_query

#: Version of the fixture file format; a file with another version fails to load.
SCHEMA_VERSION = 1


class EmbeddingFixtureError(ValueError):
    """A fixture file cannot be used; the message names the file."""


@dataclass(frozen=True)
class EmbeddingFixture:
    """A loaded fixture file.

    Attributes:
        path: Where it was read from.
        model: The embedding model that produced the vectors.
        model_source: Where the model files came from.
        model_sha256: SHA-256 of the model file.
        embedder: The library and version that ran the model.
        dimension: Length of every vector.
        vectors: Text key to vector.
    """

    path: Path
    model: str
    model_source: str
    model_sha256: str
    embedder: str
    dimension: int
    vectors: Mapping[str, list[float]]


def text_key(text: str) -> str:
    """Return the key of an embedded text."""
    return hashlib.sha256(text.encode()).hexdigest()


def encode_vector(vector: Sequence[float]) -> str:
    """Return the base64 of a vector's little-endian float32 bytes."""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()


def decode_vector(encoded: str, dimension: int) -> list[float]:
    """Decode a vector written by ``encode_vector``."""
    raw = base64.b64decode(encoded)
    if len(raw) != 4 * dimension:
        raise ValueError(f"expected {dimension} floats, got {len(raw) // 4}")
    return list(struct.unpack(f"<{dimension}f", raw))


def load_fixture(path: Path, *, model: str, dimension: int) -> EmbeddingFixture:
    """Load a fixture file and check that it fits the configured model.

    Raises:
        EmbeddingFixtureError: The file is missing or malformed, or its schema version,
            model or dimension differs from what the code expects.
    """
    try:
        data: Any = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise EmbeddingFixtureError(f"{path}: cannot read the embedding fixture: {exc}") from exc
    expected = {"schema_version": SCHEMA_VERSION, "model": model, "dimension": dimension}
    for field, value in expected.items():
        if not isinstance(data, dict) or data.get(field) != value:
            found = data.get(field) if isinstance(data, dict) else None
            raise EmbeddingFixtureError(
                f"{path}: {field} is {found!r}, expected {value!r}; "
                "regenerate it with make knowledge-fixtures"
            )
    try:
        vectors = {key: decode_vector(item, dimension) for key, item in data["vectors"].items()}
        return EmbeddingFixture(
            path=path,
            model=data["model"],
            model_source=data["model_source"],
            model_sha256=data["model_sha256"],
            embedder=data["embedder"],
            dimension=dimension,
            vectors=vectors,
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise EmbeddingFixtureError(f"{path}: malformed embedding fixture: {exc}") from exc


def write_fixture(
    path: Path,
    vectors: Mapping[str, Sequence[float]],
    *,
    model: str,
    model_source: str,
    model_sha256: str,
    embedder: str,
    dimension: int,
) -> None:
    """Write a fixture file: one vector per line, keys in sorted order."""
    header = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "model_source": model_source,
        "model_sha256": model_sha256,
        "embedder": embedder,
        "dimension": dimension,
    }
    lines = [f"  {json.dumps(k)}: {json.dumps(v)}," for k, v in header.items()]
    entries = [
        f"    {json.dumps(key)}: {json.dumps(encode_vector(vectors[key]))}"
        for key in sorted(vectors)
    ]
    body = ",\n".join(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{\n" + "\n".join(lines) + '\n  "vectors": {\n' + body + "\n  }\n}\n")


def fixture_texts(runbooks: Path, cases: Path, queries: Iterable[str]) -> dict[str, str]:
    """Return every text the fixture must cover, by key.

    The texts are the embedding text of every runbook chunk, the retrieval query of
    every evaluation case's alert, every follow-up question of a case, and the retrieval
    evaluation queries.
    """
    texts: list[str] = []
    for path in sorted(runbooks.rglob("*.md")):
        document = chunk_markdown(path.read_text(), source=str(path))
        texts += [chunk.embedding_text for chunk in document.chunks]
    for case in sorted(item for item in cases.iterdir() if item.is_dir()):
        alert_file = case / "pack" / "alert.json"
        if alert_file.exists():
            for alert in json.loads(alert_file.read_text()).get("alerts", []):
                texts.append(alert_query(alert.get("labels", {}), alert.get("annotations", {})))
        questions_file = case / "questions.yaml"
        if questions_file.exists():
            data = yaml.safe_load(questions_file.read_text()) or {}
            texts += [item["question"] for item in data.get("questions", [])]
    texts += list(queries)
    return {text_key(text): text for text in texts}


def coverage(fixture: EmbeddingFixture, texts: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Compare a fixture with the texts it must cover.

    Returns:
        The texts without a vector, and the keys of vectors no text needs, both sorted.
    """
    missing = sorted(text for key, text in texts.items() if key not in fixture.vectors)
    extra = sorted(key for key in fixture.vectors if key not in texts)
    return missing, extra
