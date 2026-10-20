"""Runbooks are chunked by H2 and H3, and no chunk exceeds the word limit."""

from __future__ import annotations

import pytest
from knowledge_fakes import RUNBOOK_A

from bianque.knowledge.chunking import MAX_WORDS, ChunkingError, chunk_markdown, word_count


def test_sections_become_chunks_under_their_heading_paths() -> None:
    document = chunk_markdown(RUNBOOK_A, source="restarts.md")
    assert document.title == "Pod Restarts"
    assert [chunk.heading_path for chunk in document.chunks] == [
        "Pod Restarts",
        "Pod Restarts > Diagnosis",
        "Pod Restarts > Diagnosis > Exit codes",
        "Pod Restarts > Remediation",
    ]
    assert document.chunks[2].content.startswith("Exit code 137")
    assert document.chunks[1].embedding_text == (
        "Pod Restarts > Diagnosis\n\n"
        "Read the previous container log to see why the container exited."
    )


def test_a_heading_without_text_of_its_own_makes_no_chunk() -> None:
    text = "# T\n\n## Only subsections\n\n### First\n\nOne.\n\n### Second\n\nTwo.\n"
    paths = [c.heading_path for c in chunk_markdown(text, source="t.md").chunks]
    assert paths == ["T > Only subsections > First", "T > Only subsections > Second"]


def test_a_new_h2_resets_the_h3() -> None:
    text = "# T\n\n## A\n\n### A1\n\nx\n\n## B\n\ny\n\n### B1\n\nz\n"
    paths = [c.heading_path for c in chunk_markdown(text, source="t.md").chunks]
    assert paths == ["T > A > A1", "T > B", "T > B > B1"]


def test_headings_inside_code_blocks_are_text() -> None:
    text = "# T\n\n## Shell\n\n```bash\n## not a heading\nkubectl get pods\n```\n"
    chunks = chunk_markdown(text, source="t.md").chunks
    assert len(chunks) == 1
    assert "## not a heading" in chunks[0].content


def test_a_long_section_splits_at_paragraphs_under_one_heading_path() -> None:
    paragraphs = [" ".join(f"w{i}x{j}" for j in range(120)) for i in range(5)]
    text = "# T\n\n## Long\n\n" + "\n\n".join(paragraphs) + "\n"
    chunks = chunk_markdown(text, source="t.md").chunks
    assert len(chunks) == 3
    assert {c.heading_path for c in chunks} == {"T > Long"}
    assert all(word_count(c.embedding_text) <= MAX_WORDS for c in chunks)
    assert "\n\n".join(c.content for c in chunks) == "\n\n".join(paragraphs)


def test_an_oversized_paragraph_splits_at_sentences() -> None:
    sentences = [" ".join(["word"] * 40) + "." for _ in range(10)]
    text = "# T\n\n## Long\n\n" + " ".join(sentences) + "\n"
    chunks = chunk_markdown(text, source="t.md").chunks
    assert len(chunks) == 2
    assert all(word_count(c.embedding_text) <= MAX_WORDS for c in chunks)
    assert all(c.content.endswith(".") for c in chunks)
    assert sum(word_count(c.content) for c in chunks) == 400


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("## No title\n\ntext\n", "heading before the H1"),
        ("intro\n# Title\n", "text before the H1"),
        ("# One\n\n# Two\n", "more than one H1"),
        ("plain\n", "text before the H1"),
        ("", "no H1"),
    ],
)
def test_a_runbook_needs_exactly_one_title_first(text: str, message: str) -> None:
    with pytest.raises(ChunkingError, match=message):
        chunk_markdown(text, source="bad.md")
