"""Split a runbook into chunks by its headings.

A runbook has one H1, its title. Each H2 and H3 section becomes a chunk, and so does the
introduction between the H1 and the first H2. A chunk's heading path is the chain of
headings from the H1 down, joined by `` > ``.

A chunk is embedded together with its heading path, and the embedding model reads at
most 512 tokens. A chunk whose embedding text exceeds ``MAX_WORDS`` words is split at
paragraph boundaries, a paragraph that alone exceeds it at sentence boundaries. The limit
counts words, not tokens: in ``fixture`` mode there is no model and so no tokenizer, and
both modes must cut the same chunks. Headings inside fenced code blocks are not headings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Words of one chunk's embedding text; English prose stays well under 512 tokens.
MAX_WORDS = 300

#: Joins the headings of a heading path.
HEADING_SEPARATOR = " > "

_HEADING_RE = re.compile(r"^(#{1,3}) +(\S.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


class ChunkingError(ValueError):
    """A runbook does not have the structure the chunker expects."""


@dataclass(frozen=True)
class Chunk:
    """A piece of a runbook under one heading path."""

    heading_path: str
    content: str

    @property
    def embedding_text(self) -> str:
        """The text that is embedded: the heading path, a blank line and the content."""
        return f"{self.heading_path}\n\n{self.content}"


@dataclass(frozen=True)
class ChunkedDocument:
    """A runbook's title and its chunks in document order."""

    title: str
    chunks: list[Chunk]


def word_count(text: str) -> int:
    """Return the number of whitespace-separated words in ``text``."""
    return len(text.split())


def chunk_markdown(text: str, *, source: str) -> ChunkedDocument:
    """Split a runbook into chunks.

    Args:
        text: The Markdown text.
        source: The file name, for error messages.

    Returns:
        The title and the chunks; sections without text produce no chunk.

    Raises:
        ChunkingError: The runbook does not have exactly one H1, or text precedes it.
    """
    title: str | None = None
    headings: list[str] = []
    body: list[str] = []
    sections: list[tuple[str, str]] = []
    in_fence = False

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            sections.append((HEADING_SEPARATOR.join(headings), content))
        body.clear()

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING_RE.match(line)
        if heading is None:
            if title is None and line.strip():
                raise ChunkingError(f"{source}: text before the H1 title")
            body.append(line)
            continue
        level, name = len(heading.group(1)), heading.group(2)
        if level == 1:
            if title is not None:
                raise ChunkingError(f"{source}: more than one H1 title")
            title, headings = name, [name]
            continue
        if title is None:
            raise ChunkingError(f"{source}: a heading before the H1 title")
        flush()
        headings = [title, *headings[1 : level - 1], name] if level == 3 else [title, name]
    if title is None:
        raise ChunkingError(f"{source}: no H1 title")
    flush()
    chunks = [
        piece for heading_path, content in sections for piece in _split(heading_path, content)
    ]
    return ChunkedDocument(title=title, chunks=chunks)


def _split(heading_path: str, content: str) -> list[Chunk]:
    """Split one section's content so that every chunk stays within ``MAX_WORDS``."""
    budget = MAX_WORDS - word_count(heading_path)
    if budget <= 0:
        raise ChunkingError(f"heading path too long: {heading_path!r}")
    units: list[str] = []
    for paragraph in _paragraphs(content):
        if word_count(paragraph) <= budget:
            units.append(paragraph)
        else:
            units.extend(_pieces(paragraph, budget))
    chunks: list[str] = []
    current: list[str] = []
    for unit in units:
        if current and word_count("\n\n".join([*current, unit])) > budget:
            chunks.append("\n\n".join(current))
            current = []
        current.append(unit)
    if current:
        chunks.append("\n\n".join(current))
    return [Chunk(heading_path, chunk) for chunk in chunks]


def _paragraphs(content: str) -> list[str]:
    """Split content at blank lines, keeping each fenced code block in one piece."""
    paragraphs: list[str] = []
    lines: list[str] = []
    in_fence = False
    for line in content.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if lines:
                paragraphs.append("\n".join(lines))
                lines = []
            continue
        lines.append(line)
    if lines:
        paragraphs.append("\n".join(lines))
    return paragraphs


def _pieces(paragraph: str, budget: int) -> list[str]:
    """Split an oversized paragraph at sentence ends, and a sentence at word count."""
    sentences: list[str] = []
    for sentence in _SENTENCE_END_RE.split(paragraph):
        words = sentence.split()
        for start in range(0, len(words), budget):
            sentences.append(" ".join(words[start : start + budget]))
    pieces: list[str] = []
    current: list[str] = []
    for unit in sentences:
        if current and word_count(" ".join([*current, unit])) > budget:
            pieces.append(" ".join(current))
            current = []
        current.append(unit)
    if current:
        pieces.append(" ".join(current))
    return pieces
