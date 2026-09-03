from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

ACTIVITY_SEGMENTS = {
    "cold_start": {"minimum_history": 1, "maximum_history": 2},
    "active": {"minimum_history": 3, "maximum_history": 9},
    "highly_active": {"minimum_history": 10, "maximum_history": None},
}


def _validate_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")


def recall_at_k(ranked_items: Sequence[str], relevant_items: set[str], k: int) -> float:
    _validate_k(k)
    if not relevant_items:
        return 0.0
    return len(set(ranked_items[:k]) & relevant_items) / len(relevant_items)


def hit_rate_at_k(ranked_items: Sequence[str], relevant_items: set[str], k: int) -> float:
    _validate_k(k)
    return float(bool(set(ranked_items[:k]) & relevant_items))


def ndcg_at_k(ranked_items: Sequence[str], relevant_items: set[str], k: int) -> float:
    _validate_k(k)
    if not relevant_items:
        return 0.0
    dcg = sum(
        1.0 / math.log2(rank + 2)
        for rank, item_id in enumerate(ranked_items[:k])
        if item_id in relevant_items
    )
    ideal = sum(1.0 / math.log2(rank + 2) for rank in range(min(k, len(relevant_items))))
    return dcg / ideal if ideal else 0.0


def binary_auc(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Tie-aware Mann-Whitney AUC without an external metrics dependency."""

    if len(labels) != len(scores) or not labels:
        raise ValueError("labels and scores must be non-empty and have equal length")
    if any(label not in {0, 1} for label in labels):
        raise ValueError("AUC labels must be binary")
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        raise ValueError("AUC requires both positive and negative labels")
    ordered = sorted(enumerate(scores), key=lambda row: (row[1], row[0]))
    rank_sum = 0.0
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        average_rank = ((cursor + 1) + end) / 2
        rank_sum += average_rank * sum(labels[index] for index, _score in ordered[cursor:end])
        cursor = end
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def aggregate_ranking_metrics(
    rankings: Mapping[str, Sequence[str]],
    relevant: Mapping[str, set[str]],
    k_values: Iterable[int],
) -> dict[str, float]:
    users = sorted(set(rankings) & set(relevant))
    if not users:
        raise ValueError("ranking metrics require at least one evaluated user")
    output: dict[str, float] = {}
    for k in sorted(set(k_values)):
        _validate_k(k)
        output[f"recall@{k}"] = sum(
            recall_at_k(rankings[user], relevant[user], k) for user in users
        ) / len(users)
        output[f"ndcg@{k}"] = sum(
            ndcg_at_k(rankings[user], relevant[user], k) for user in users
        ) / len(users)
        output[f"hit_rate@{k}"] = sum(
            hit_rate_at_k(rankings[user], relevant[user], k) for user in users
        ) / len(users)
    return output


class RankingMetricAccumulator:
    """Accumulate top-K ranking metrics without retaining per-user rankings."""

    def __init__(self, k_values: Iterable[int]) -> None:
        self.k_values = tuple(sorted(set(k_values)))
        if not self.k_values:
            raise ValueError("ranking metrics require at least one K")
        for k in self.k_values:
            _validate_k(k)
        self.user_count = 0
        self._totals = {
            name: 0.0
            for k in self.k_values
            for name in (f"recall@{k}", f"ndcg@{k}", f"hit_rate@{k}")
        }

    def add(self, ranked_items: Sequence[str], relevant_items: set[str]) -> None:
        self.user_count += 1
        for k in self.k_values:
            self._totals[f"recall@{k}"] += recall_at_k(ranked_items, relevant_items, k)
            self._totals[f"ndcg@{k}"] += ndcg_at_k(ranked_items, relevant_items, k)
            self._totals[f"hit_rate@{k}"] += hit_rate_at_k(ranked_items, relevant_items, k)

    def result(self) -> dict[str, float]:
        if self.user_count == 0:
            raise ValueError("ranking metrics require at least one evaluated user")
        return {name: value / self.user_count for name, value in sorted(self._totals.items())}


def activity_segment(history_length: int) -> str:
    if (
        isinstance(history_length, bool)
        or not isinstance(history_length, int)
        or history_length < 1
    ):
        raise ValueError("history length must be a positive integer")
    if history_length <= 2:
        return "cold_start"
    if history_length <= 9:
        return "active"
    return "highly_active"


def aggregate_segmented_ranking_metrics(
    rankings: Mapping[str, Sequence[str]],
    relevant: Mapping[str, set[str]],
    history_lengths: Mapping[str, int],
    k_values: Iterable[int],
) -> dict[str, dict[str, float | int]]:
    grouped_rankings: dict[str, dict[str, Sequence[str]]] = defaultdict(dict)
    grouped_relevant: dict[str, dict[str, set[str]]] = defaultdict(dict)
    for user_id in sorted(set(rankings) & set(relevant)):
        if user_id not in history_lengths:
            raise ValueError(f"missing activity history length for user {user_id}")
        segment = activity_segment(history_lengths[user_id])
        grouped_rankings[segment][user_id] = rankings[user_id]
        grouped_relevant[segment][user_id] = relevant[user_id]

    ordered_k = tuple(sorted(set(k_values)))
    output: dict[str, dict[str, float | int]] = {}
    for segment in ACTIVITY_SEGMENTS:
        users = grouped_rankings.get(segment, {})
        metrics = (
            aggregate_ranking_metrics(users, grouped_relevant[segment], ordered_k)
            if users
            else {
                name: 0.0
                for k in ordered_k
                for name in (f"recall@{k}", f"ndcg@{k}", f"hit_rate@{k}")
            }
        )
        output[segment] = {"user_count": len(users), **metrics}
    return output
