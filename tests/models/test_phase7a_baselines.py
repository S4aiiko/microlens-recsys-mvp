from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from recsys.data.artifacts import JsonLinesCodec
from recsys.models.baselines import (
    evaluate_baselines,
    popularity_rankings,
    random_rankings,
    relevant_by_user,
)
from recsys.models.data import load_model_data
from recsys.models.metrics import (
    ACTIVITY_SEGMENTS,
    activity_segment,
    aggregate_ranking_metrics,
    aggregate_segmented_ranking_metrics,
)

from ._support import model_config, write_data_version


def _fixture_data():
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    version, checksum = write_data_version(root / "processed")
    data = load_model_data(
        processed_root=root / "processed",
        data_version=version,
        data_manifest_checksum=checksum,
        title_config=model_config()["title"],
        codec=JsonLinesCodec(),
    )
    return temporary, data


def test_streaming_baselines_are_exactly_equivalent_to_complete_rankings() -> None:
    temporary, data = _fixture_data()
    try:
        relevant = relevant_by_user(data.test)
        expected = {
            "random": aggregate_ranking_metrics(
                random_rankings(data, set(relevant), seed=71), relevant, [1, 3]
            ),
            "popularity": aggregate_ranking_metrics(
                popularity_rankings(data, set(relevant)), relevant, [1, 3]
            ),
        }
        with (
            mock.patch(
                "recsys.models.baselines.random_rankings",
                side_effect=AssertionError("must not materialize all random rankings"),
            ),
            mock.patch(
                "recsys.models.baselines.popularity_rankings",
                side_effect=AssertionError("must not materialize all popularity rankings"),
            ),
        ):
            actual = evaluate_baselines(data, split="test", k_values=[1, 3], seed=71)
        assert actual == expected
    finally:
        temporary.cleanup()


def test_full_catalog_baseline_passes_only_a_generator_and_max_k_to_heap() -> None:
    item_ids = tuple(f"item-{index:05d}" for index in range(19_220))
    data = SimpleNamespace(
        item_ids=item_ids,
        user_train_items={"user": (item_ids[0],)},
        train_popularity={item_id: float(index % 17) for index, item_id in enumerate(item_ids)},
        test=({"user_id": "user", "item_id": item_ids[-1]},),
    )
    calls: list[tuple[int, int, bool]] = []

    def bounded_nsmallest(n, iterable, *, key):
        assert not isinstance(iterable, (list, tuple))
        rows = list(iterable)
        calls.append((n, len(rows), True))
        return sorted(rows, key=key)[:n]

    with mock.patch("recsys.models.baselines.heapq.nsmallest", side_effect=bounded_nsmallest):
        result = evaluate_baselines(data, split="test", k_values=[5, 10, 20], seed=7)

    assert set(result) == {"random", "popularity"}
    assert calls == [(20, 19_219, True), (20, 19_219, True)]


def test_activity_segment_boundaries_and_segmented_metrics() -> None:
    assert ACTIVITY_SEGMENTS == {
        "cold_start": {"minimum_history": 1, "maximum_history": 2},
        "active": {"minimum_history": 3, "maximum_history": 9},
        "highly_active": {"minimum_history": 10, "maximum_history": None},
    }
    assert [activity_segment(value) for value in (1, 2, 3, 9, 10, 100)] == [
        "cold_start",
        "cold_start",
        "active",
        "active",
        "highly_active",
        "highly_active",
    ]
    rankings = {"u1": ["a"], "u2": ["b"], "u3": ["c"]}
    relevant = {"u1": {"a"}, "u2": {"x"}, "u3": {"c"}}
    segmented = aggregate_segmented_ranking_metrics(
        rankings, relevant, {"u1": 2, "u2": 3, "u3": 10}, [1]
    )
    assert segmented["cold_start"] == {
        "user_count": 1,
        "recall@1": 1.0,
        "ndcg@1": 1.0,
        "hit_rate@1": 1.0,
    }
    assert segmented["active"]["recall@1"] == 0.0
    assert segmented["highly_active"]["hit_rate@1"] == 1.0
