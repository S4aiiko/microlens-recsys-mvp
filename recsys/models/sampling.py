from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .text import EncodedTitle, sparse_cosine

NEGATIVE_STRATEGIES = {"uniform", "popularity_aware", "train_only_hard"}


def stable_seed(seed: int, *parts: str) -> int:
    payload = "\0".join((str(seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


@dataclass(frozen=True, slots=True)
class TrainOnlyNegativeSampler:
    train_item_ids: tuple[str, ...]
    popularity: Mapping[str, float]
    title_features: Mapping[str, EncodedTitle]
    alpha: float = 0.75

    def __post_init__(self) -> None:
        if not self.train_item_ids:
            raise ValueError("negative sampler requires train items")
        if self.alpha < 0:
            raise ValueError("popularity alpha must be non-negative")
        if any(item_id not in self.title_features for item_id in self.train_item_ids):
            raise ValueError("hard-negative title features must cover every train item")

    def sample(
        self,
        *,
        user_id: str,
        positive_item_id: str,
        seen_item_ids: set[str],
        count: int,
        seed: int,
        strategy: str,
    ) -> list[str]:
        if strategy not in NEGATIVE_STRATEGIES:
            raise ValueError(f"unsupported negative strategy: {strategy}")
        if count <= 0:
            return []
        candidates = [
            item_id
            for item_id in self.train_item_ids
            if item_id != positive_item_id and item_id not in seen_item_ids
        ]
        if not candidates:
            return []
        count = min(count, len(candidates))
        rng = random.Random(stable_seed(seed, user_id, positive_item_id, strategy))
        if strategy == "uniform":
            return rng.sample(candidates, count)
        if strategy == "popularity_aware":
            remaining = candidates.copy()
            output: list[str] = []
            while remaining and len(output) < count:
                weights = [
                    max(0.0, float(self.popularity.get(item_id, 0.0))) ** self.alpha
                    for item_id in remaining
                ]
                total = sum(weights)
                if total == 0:
                    index = rng.randrange(len(remaining))
                else:
                    point = rng.random() * total
                    cumulative = 0.0
                    index = len(remaining) - 1
                    for candidate_index, weight in enumerate(weights):
                        cumulative += weight
                        if point < cumulative:
                            index = candidate_index
                            break
                output.append(remaining.pop(index))
            return output
        positive_title = self.title_features[positive_item_id]
        pool_limit = max(256, count * 64)
        if len(candidates) > pool_limit:
            random_pool = rng.sample(candidates, pool_limit)
            popularity_pool = sorted(
                candidates,
                key=lambda item_id: (-float(self.popularity.get(item_id, 0.0)), item_id),
            )[:64]
            candidates = sorted(set(random_pool + popularity_pool))
        return sorted(
            candidates,
            key=lambda item_id: (
                -sparse_cosine(positive_title, self.title_features[item_id]),
                -float(self.popularity.get(item_id, 0.0)),
                item_id,
            ),
        )[:count]


def deterministic_random_ranking(item_ids: Sequence[str], *, user_id: str, seed: int) -> list[str]:
    return sorted(
        item_ids,
        key=lambda item_id: (
            hashlib.sha256(f"{seed}\0{user_id}\0{item_id}".encode()).digest(),
            item_id,
        ),
    )
