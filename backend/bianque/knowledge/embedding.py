"""Embeddings of runbook chunks and queries, in two modes.

``local`` runs ``BAAI/bge-small-en-v1.5`` with fastembed (ONNX, 384 dimensions) from
files on disk; it never downloads. ``bianque knowledge download-model`` fetches the
files once and checks the model file's SHA-256 against the value pinned here, so an
upstream change to the files cannot silently change the model. ``fixture`` reads the
vectors of an embedding fixture and fails for a text the fixture does not cover.

Embeddings never go through the model service. This is the only module that imports
fastembed (an import-linter contract).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from functools import cached_property
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from bianque.config import Settings
from bianque.knowledge.fixtures import EmbeddingFixture, load_fixture, text_key
from bianque.storage.models import EMBEDDING_DIMENSION

if TYPE_CHECKING:
    from fastembed import TextEmbedding
    from tokenizers import Tokenizer

#: The embedding model.
MODEL_NAME = "BAAI/bge-small-en-v1.5"

#: The Hugging Face repository fastembed downloads the model from.
MODEL_SOURCE = "Qdrant/bge-small-en-v1.5-onnx-Q"

#: The model file inside that repository, and its SHA-256.
MODEL_FILE = "model_optimized.onnx"
MODEL_SHA256 = "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"

#: Input tokens the model reads; longer input is truncated.
MAX_TOKENS = 512


class EmbeddingError(RuntimeError):
    """A text cannot be embedded."""


class ModelUnavailableError(EmbeddingError):
    """The local model files are missing or are not the pinned ones."""


class FixtureMissError(EmbeddingError):
    """The embedding fixture has no vector for a text."""


class Embedder(Protocol):
    """Turns texts into vectors."""

    @property
    def model(self) -> str:
        """The embedding model's name."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in order."""
        ...


def embedder_version() -> str:
    """Return the library and version that runs the local model."""
    return f"fastembed {version('fastembed')}"


def find_model_file(model_dir: Path) -> Path | None:
    """Return the model file under ``model_dir``, or ``None`` when it is not there."""
    repository = model_dir / f"models--{MODEL_SOURCE.replace('/', '--')}"
    return next(iter(sorted(repository.glob(f"snapshots/*/{MODEL_FILE}"))), None)


def file_sha256(path: Path) -> str:
    """Return the SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download_model(model_dir: Path) -> Path:
    """Download the model into ``model_dir`` and check its SHA-256.

    Returns:
        The model file.

    Raises:
        ModelUnavailableError: The downloaded file is not the pinned one.
    """
    from fastembed import TextEmbedding

    TextEmbedding(MODEL_NAME, cache_dir=str(model_dir))
    path = find_model_file(model_dir)
    if path is None:
        raise ModelUnavailableError(f"{MODEL_FILE} not found under {model_dir} after download")
    digest = file_sha256(path)
    if digest != MODEL_SHA256:
        raise ModelUnavailableError(
            f"{path} has SHA-256 {digest}, expected {MODEL_SHA256}: the upstream model changed"
        )
    return path


class LocalEmbedder:
    """Runs the embedding model from local files; loads it on first use."""

    model = MODEL_NAME

    def __init__(self, model_dir: Path) -> None:
        """Use the model files under ``model_dir`` (see ``download_model``)."""
        self._model_dir = model_dir

    def _model_file(self) -> Path:
        path = find_model_file(self._model_dir)
        if path is None:
            raise ModelUnavailableError(
                f"no embedding model under {self._model_dir}: run bianque knowledge download-model"
            )
        return path

    @cached_property
    def _embedding(self) -> TextEmbedding:
        from fastembed import TextEmbedding

        self._model_file()
        return TextEmbedding(MODEL_NAME, cache_dir=str(self._model_dir), local_files_only=True)

    @cached_property
    def _tokenizer(self) -> Tokenizer:
        from tokenizers import Tokenizer

        return Tokenizer.from_file(str(self._model_file().parent / "tokenizer.json"))

    def model_sha256(self) -> str:
        """Return the SHA-256 of the model file in use."""
        return file_sha256(self._model_file())

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text; the model runs in a worker thread."""
        batch = list(texts)

        def run() -> list[list[float]]:
            return [[float(x) for x in vector] for vector in self._embedding.embed(batch)]

        return await asyncio.to_thread(run)

    def token_count(self, text: str) -> int:
        """Return the number of model tokens of ``text``, without truncation."""
        tokenizer = self._tokenizer
        tokenizer.no_truncation()
        return len(tokenizer.encode(text).ids)


class FixtureEmbedder:
    """Reads vectors from an embedding fixture; loads the file on first use."""

    model = MODEL_NAME

    def __init__(self, path: Path) -> None:
        """Read the fixture at ``path``."""
        self._path = path

    @cached_property
    def fixture(self) -> EmbeddingFixture:
        """The loaded fixture."""
        return load_fixture(self._path, model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return the fixture's vector of each text.

        Raises:
            FixtureMissError: A text has no vector in the fixture.
        """
        vectors = self.fixture.vectors
        result = []
        for text in texts:
            vector = vectors.get(text_key(text))
            if vector is None:
                raise FixtureMissError(
                    f"{self._path}: no vector for the text starting {text[:60]!r}; "
                    "regenerate it with make knowledge-fixtures"
                )
            result.append(vector)
        return result


def build_embedder(settings: Settings) -> Embedder:
    """Return the embedder selected by ``BIANQUE_EMBEDDING_MODE``."""
    if settings.embedding_mode == "local":
        return LocalEmbedder(settings.embedding_model_dir)
    return FixtureEmbedder(settings.embedding_fixture)
