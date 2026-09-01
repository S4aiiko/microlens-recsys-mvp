from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from .data import ModelData
from .metrics import aggregate_ranking_metrics
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


def evaluate_baselines(
    data: ModelData, *, split: str, k_values: list[int], seed: int
) -> dict[str, Mapping[str, float]]:
    if split not in {"validation", "test"}:
        raise ValueError("baseline split must be validation or test")
    relevant = relevant_by_user(getattr(data, split))
    users = set(relevant)
    return {
        "random": aggregate_ranking_metrics(
            random_rankings(data, users, seed=seed), relevant, k_values
        ),
        "popularity": aggregate_ranking_metrics(
            popularity_rankings(data, users), relevant, k_values
        ),
    }
