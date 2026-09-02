from __future__ import annotations

import math
import uuid

import pytest

from apps.api.app.feeds.domain import RankedCandidate, RecallCandidate
from apps.api.app.feeds.ranking import (
    PromotionPlacement,
    apply_promotions,
    merge_recall,
    min_max,
    mmr_rank,
    topic_deduplicate,
)
from apps.api.app.feeds.retrieval import ItemItemIndex


def candidate(
    item_id: str,
    score: float,
    rank: int,
    *,
    topic: str | None = None,
) -> RankedCandidate:
    return RankedCandidate(
        item_id=item_id,
        title=item_id,
        cover=None,
        source="dssm",
        sources=("dssm",),
        raw_score=score,
        normalized_score=score,
        score=score,
        reason="fixture",
        original_rank=rank,
        title_topic=topic or f"topic:{item_id}",
    )


def test_minmax_equal_scores_and_multi_source_merge_are_deterministic() -> None:
    assert min_max([2.0, 2.0]) == [1.0, 1.0]
    merged = merge_recall(
        [
            RecallCandidate("b", "popular", 1.0, "p"),
            RecallCandidate("a", "popular", 2.0, "p"),
            RecallCandidate("b", "dssm", 4.0, "d"),
            RecallCandidate("c", "dssm", 1.0, "d"),
        ]
    )
    assert [row.item_id for row in merged] == ["b", "a", "c"]
    assert merged[0].sources == ("dssm", "popular")
    assert merged[0].normalized_score == 1.0


def test_hand_calculated_mmr_oracle_promotes_distinct_title() -> None:
    rows = [
        candidate("near-a", 1.0, 0),
        candidate("near-b", 0.9, 1),
        candidate("distinct", 0.89, 2),
    ]
    vectors = {
        "near-a": (1.0, 0.0),
        "near-b": (0.995, 0.1),
        "distinct": (0.0, 1.0),
    }
    ranked, steps = mmr_rank(rows, vectors=vectors, lambda_value=0.75)
    assert [row.item_id for row in ranked] == ["near-a", "distinct", "near-b"]
    assert steps[0].normalized_relevance == pytest.approx(1.0)
    assert steps[0].max_similarity == 0.0
    assert steps[0].mmr_score == pytest.approx(0.75)
    assert steps[1].normalized_relevance == 0.0
    assert steps[1].max_similarity == 0.0
    assert steps[1].mmr_score == 0.0
    expected_similarity = 0.995 / math.sqrt(0.995**2 + 0.1**2)
    expected_relevance = (0.9 - 0.89) / (1.0 - 0.89)
    assert steps[2].max_similarity == pytest.approx(expected_similarity)
    assert steps[2].mmr_score == pytest.approx(
        0.75 * expected_relevance - 0.25 * expected_similarity
    )
    reranked, repeated_steps = mmr_rank(rows, vectors=vectors, lambda_value=0.75)
    assert reranked == ranked
    assert repeated_steps == steps


def test_mmr_clamps_negative_cosine_and_missing_vector_uses_original_score() -> None:
    rows = [candidate("vector", 0.9, 0), candidate("missing", 0.8, 1)]
    ranked, steps = mmr_rank(rows, vectors={"vector": (-1.0, 0.0), "missing": None})
    assert [row.item_id for row in ranked] == ["missing", "vector"]
    assert steps[0].mmr_score == 0.8
    assert steps[0].fallback_reason == "missing_title_vector_original_score"
    assert steps[1].max_similarity == 0.0


def test_topic_dedup_and_mmr_are_independently_switchable() -> None:
    rows = [
        candidate("a", 1.0, 0, topic="same"),
        candidate("b", 0.9, 1, topic="same"),
        candidate("c", 0.8, 2, topic="other"),
    ]
    kept, removed = topic_deduplicate(rows)
    assert [row.item_id for row in kept] == ["a", "c"]
    assert [row.item_id for row in removed] == ["b"]
    mmr_only, _steps = mmr_rank(
        rows,
        vectors={"a": (1.0, 0.0), "b": (1.0, 0.0), "c": (0.0, 1.0)},
        lambda_value=0.5,
    )
    assert {row.item_id for row in mmr_only} == {"a", "b", "c"}


def test_train_history_item_item_cosine_and_promotion_priority() -> None:
    index = ItemItemIndex.from_histories(
        {"u1": ["a", "b", "c"], "u2": ["a", "b"], "u3": ["a", "d"]}
    )
    recalled = index.recall(["a"], top_n=3)
    assert [row.item_id for row in recalled] == ["b", "c", "d"]
    assert recalled[0].raw_score > recalled[1].raw_score

    natural = [candidate("n1", 1.0, 0), candidate("n2", 0.9, 1)]
    high = PromotionPlacement(uuid.UUID(int=1), "p-high", 10, 0, "high")
    low = PromotionPlacement(uuid.UUID(int=2), "p-low", 1, 0, "low")
    output = apply_promotions(
        natural,
        placements=[low, high],
        promoted_candidates={
            "p-high": candidate("p-high", 0.0, 2),
            "p-low": candidate("p-low", 0.0, 3),
        },
    )
    assert [row.item_id for row in output] == ["p-high", "p-low", "n1", "n2"]

    second_position = PromotionPlacement(uuid.UUID(int=3), "p-second", 5, 1, "second")
    positioned = apply_promotions(
        natural,
        placements=[high, second_position],
        promoted_candidates={
            "p-high": candidate("p-high", 0.0, 2),
            "p-second": candidate("p-second", 0.0, 3),
        },
    )
    assert [row.item_id for row in positioned] == ["p-high", "p-second", "n1", "n2"]
