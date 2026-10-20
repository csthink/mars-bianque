"""Fusion follows the RRF formula exactly."""

from __future__ import annotations

import pytest

from bianque.knowledge.search import Fused, rrf


def test_fused_scores_and_order_match_the_formula() -> None:
    vector = ["kn_a", "kn_b", "kn_c"]
    text = ["kn_c", "kn_d", "kn_a"]
    fused = rrf(vector, text, k=60, limit=5)
    expected = {
        "kn_a": 1 / 61 + 1 / 63,
        "kn_c": 1 / 63 + 1 / 61,
        "kn_b": 1 / 62,
        "kn_d": 1 / 62,
    }
    # kn_a and kn_c tie, and so do kn_b and kn_d: ties go by knowledge ID.
    assert [hit.knowledge_id for hit in fused] == ["kn_a", "kn_c", "kn_b", "kn_d"]
    for hit in fused:
        assert hit.score == pytest.approx(expected[hit.knowledge_id], abs=0)
    assert fused[0] == Fused("kn_a", 1, 3, expected["kn_a"])
    assert fused[2] == Fused("kn_b", 2, None, 1 / 62)
    assert fused[3] == Fused("kn_d", None, 2, 1 / 62)


def test_only_the_top_results_are_kept() -> None:
    vector = [f"kn_{i:02d}" for i in range(20)]
    fused = rrf(vector, [], limit=5)
    assert [hit.knowledge_id for hit in fused] == vector[:5]
    assert [hit.vector_rank for hit in fused] == [1, 2, 3, 4, 5]
    assert all(hit.text_rank is None for hit in fused)


def test_one_empty_ranking_leaves_the_other() -> None:
    assert [h.knowledge_id for h in rrf([], ["kn_x", "kn_y"])] == ["kn_x", "kn_y"]
    assert rrf([], []) == []
