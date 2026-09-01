from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import torch

from .data import ModelData
from .text import EncodedTitle, sparse_cosine

DENSE_FEATURE_NAMES = (
    "dssm_recall_score",
    "title_history_similarity",
    "train_popularity_log_normalized",
    "train_novelty",
    "train_user_activity_log_normalized",
    "train_time_decay_weight",
)


def padded_title_tables(
    rows: Sequence[EncodedTitle], *, maximum_tokens: int
) -> tuple[torch.Tensor, torch.Tensor]:
    if maximum_tokens < 1:
        raise ValueError("maximum title tokens must be positive")
    token_rows: list[list[int]] = []
    weight_rows: list[list[float]] = []
    for row in rows:
        selected = sorted(
            zip(row.token_ids, row.weights, strict=True), key=lambda pair: (-pair[1], pair[0])
        )[:maximum_tokens]
        selected.sort()
        tokens = [token for token, _weight in selected]
        weights = [weight for _token, weight in selected]
        missing = maximum_tokens - len(tokens)
        token_rows.append(tokens + [0] * missing)
        weight_rows.append(weights + [0.0] * missing)
    return torch.tensor(token_rows, dtype=torch.long), torch.tensor(
        weight_rows, dtype=torch.float32
    )


@dataclass(frozen=True, slots=True)
class FeatureIndex:
    data: ModelData
    title_enabled: bool
    user_to_index: Mapping[str, int]
    item_to_index: Mapping[str, int]
    normalized_popularity: Mapping[str, float]
    normalized_activity: Mapping[str, float]

    @classmethod
    def build(cls, data: ModelData, *, title_enabled: bool = True) -> FeatureIndex:
        maximum_popularity = max(
            (math.log1p(max(0.0, value)) for value in data.train_popularity.values()),
            default=1.0,
        )
        maximum_activity = max(
            (math.log1p(len(history)) for history in data.user_train_items.values()), default=1.0
        )
        return cls(
            data=data,
            title_enabled=title_enabled,
            user_to_index={user_id: index for index, user_id in enumerate(data.user_ids)},
            item_to_index={item_id: index for index, item_id in enumerate(data.item_ids)},
            normalized_popularity={
                item_id: math.log1p(max(0.0, data.train_popularity.get(item_id, 0.0)))
                / maximum_popularity
                for item_id in data.item_ids
            },
            normalized_activity={
                user_id: math.log1p(len(data.user_train_items[user_id])) / maximum_activity
                for user_id in data.user_ids
            },
        )

    def dense(
        self,
        *,
        user_id: str,
        item_id: str,
        recall_score: float,
        time_decay_weight: float = 1.0,
    ) -> tuple[float, ...]:
        popularity = self.normalized_popularity[item_id]
        title_similarity = (
            sparse_cosine(self.data.user_history_titles[user_id], self.data.encoded_titles[item_id])
            if self.title_enabled
            else 0.0
        )
        return (
            float(recall_score),
            float(title_similarity),
            float(popularity),
            float(1.0 - popularity),
            float(self.normalized_activity[user_id]),
            float(time_decay_weight),
        )
