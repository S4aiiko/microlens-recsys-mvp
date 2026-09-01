from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence


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
