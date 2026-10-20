"""The embedding fixture round-trips exactly and refuses a mismatched file."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from knowledge_fakes import fake_vector

from bianque.knowledge.embedding import MODEL_NAME, FixtureEmbedder, FixtureMissError
from bianque.knowledge.fixtures import (
    EmbeddingFixtureError,
    coverage,
    load_fixture,
    text_key,
    write_fixture,
)
from bianque.storage.models import EMBEDDING_DIMENSION


def _write(path: Path, texts: list[str]) -> dict[str, list[float]]:
    vectors = {text_key(text): fake_vector(text) for text in texts}
    write_fixture(
        path,
        vectors,
        model=MODEL_NAME,
        model_source="source",
        model_sha256="0" * 64,
        embedder="fastembed 0.9.0",
        dimension=EMBEDDING_DIMENSION,
    )
    return vectors


def test_vectors_read_back_as_the_float32_values_written(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    vectors = _write(path, ["alpha", "beta"])
    fixture = load_fixture(path, model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)
    assert set(fixture.vectors) == set(vectors)
    for key, vector in vectors.items():
        as_float32 = list(
            struct.unpack(f"<{len(vector)}f", struct.pack(f"<{len(vector)}f", *vector))
        )
        assert fixture.vectors[key] == as_float32
    lines = path.read_text().splitlines()
    assert lines[1] == '  "schema_version": 1,'
    assert sum(1 for line in lines if line.startswith('    "')) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("model", "other/model"), ("dimension", 768)],
)
def test_a_fixture_that_does_not_fit_fails_and_names_the_file(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "fixture.json"
    _write(path, ["alpha"])
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(EmbeddingFixtureError, match=f"{path}: {field}"):
        load_fixture(path, model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)


def test_a_missing_or_malformed_fixture_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(EmbeddingFixtureError, match=str(tmp_path / "absent.json")):
        load_fixture(tmp_path / "absent.json", model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)
    path = tmp_path / "fixture.json"
    _write(path, ["alpha"])
    data = json.loads(path.read_text())
    data["vectors"]["x"] = "AAAA"
    path.write_text(json.dumps(data))
    with pytest.raises(EmbeddingFixtureError, match=f"{path}: malformed"):
        load_fixture(path, model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)


async def test_the_fixture_embedder_fails_for_a_text_it_does_not_cover(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    vectors = _write(path, ["alpha"])
    embedder = FixtureEmbedder(path)
    assert await embedder.embed(["alpha"]) == [embedder.fixture.vectors[text_key("alpha")]]
    assert len(vectors) == 1
    with pytest.raises(FixtureMissError, match="no vector for the text starting 'gamma'"):
        await embedder.embed(["alpha", "gamma"])


def test_coverage_reports_missing_texts_and_unneeded_vectors(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    _write(path, ["alpha", "stale"])
    fixture = load_fixture(path, model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)
    texts = {text_key(t): t for t in ["alpha", "new"]}
    assert coverage(fixture, texts) == (["new"], [text_key("stale")])
