from __future__ import annotations

import hashlib
import heapq
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping

from .data import ModelData
from .metrics import ACTIVITY_SEGMENTS, RankingMetricAccumulator, activity_segment
from .sampling import deterministic_random_ranking


def relevant_by_user(rows: tuple[dict[str, object], ...]) -> dict[str, set[str]]:
    output: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        output[str(row["user_id"])].add(str(row["item_id"]))
    return dict(output)


def eligible_catalog(data: ModelData, user_id: str) -> list[str]:
    seen = set(data.user_train_items[user_id])
    return [item_id for item_id in data.item_ids if item_id not in seen]


def popularity_rankings(data: ModelData, users: set[str]) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for user_id in sorted(users):
        output[user_id] = sorted(
            eligible_catalog(data, user_id),
            key=lambda item_id: (-float(data.train_popularity.get(item_id, 0.0)), item_id),
        )
    return output


def random_rankings(data: ModelData, users: set[str], *, seed: int) -> dict[str, list[str]]:
    return {
        user_id: deterministic_random_ranking(
            eligible_catalog(data, user_id), user_id=user_id, seed=seed
        )
        for user_id in sorted(users)
    }


def _top_k_eligible(
    data: ModelData,
    user_id: str,
    *,
    top_k: int,
    key: Callable[[str], object],
) -> list[str]:
    seen = set(data.user_train_items[user_id])
    eligible: Iterable[str] = (item_id for item_id in data.item_ids if item_id not in seen)
    return heapq.nsmallest(top_k, eligible, key=key)


def _streaming_baseline_metrics(
    data: ModelData,
    relevant: Mapping[str, set[str]],
    k_values: list[int],
    *,
    key_for_user: Callable[[str], Callable[[str], object]],
) -> tuple[dict[str, float], dict[str, dict[str, float | int]]]:
    maximum_k = max(k_values)
    accumulator = RankingMetricAccumulator(k_values)
    segment_accumulators = {name: RankingMetricAccumulator(k_values) for name in ACTIVITY_SEGMENTS}
    for user_id in sorted(relevant):
        ranking = _top_k_eligible(
            data,
            user_id,
            top_k=maximum_k,
            key=key_for_user(user_id),
        )
        accumulator.add(ranking, relevant[user_id])
        segment_accumulators[activity_segment(len(data.user_train_items[user_id]))].add(
            ranking, relevant[user_id]
        )
    segments: dict[str, dict[str, float | int]] = {}
    for name, segment_accumulator in segment_accumulators.items():
        metrics = (
            segment_accumulator.result()
            if segment_accumulator.user_count
            else {
                metric_name: 0.0
                for k in sorted(set(k_values))
                for metric_name in (f"recall@{k}", f"ndcg@{k}", f"hit_rate@{k}")
            }
        )
        segments[name] = {"user_count": segment_accumulator.user_count, **metrics}
    return accumulator.result(), segments


def evaluate_baselines_with_segments(
    data: ModelData, *, split: str, k_values: list[int], seed: int
) -> tuple[
    dict[str, Mapping[str, float]],
    dict[str, dict[str, dict[str, float | int]]],
]:
    if split not in {"validation", "test"}:
        raise ValueError("baseline split must be validation or test")
    relevant = relevant_by_user(getattr(data, split))
    random_metrics, random_segments = _streaming_baseline_metrics(
        data,
        relevant,
        k_values,
        key_for_user=lambda user_id: (
            lambda item_id: (
                hashlib.sha256(f"{seed}\0{user_id}\0{item_id}".encode()).digest(),
                item_id,
            )
        ),
    )
    popularity_metrics, popularity_segments = _streaming_baseline_metrics(
        data,
        relevant,
        k_values,
        key_for_user=lambda _user_id: (
            lambda item_id: (
                -float(data.train_popularity.get(item_id, 0.0)),
                item_id,
            )
        ),
    )
    return (
        {"random": random_metrics, "popularity": popularity_metrics},
        {"random": random_segments, "popularity": popularity_segments},
    )


def evaluate_baselines(
    data: ModelData, *, split: str, k_values: list[int], seed: int
) -> dict[str, Mapping[str, float]]:
    metrics, _segments = evaluate_baselines_with_segments(
        data, split=split, k_values=k_values, seed=seed
    )
    return metrics
