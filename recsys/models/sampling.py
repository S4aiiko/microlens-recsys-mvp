from __future__ import annotations

import hashlib
import heapq
import math
import random
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .text import EncodedTitle, sparse_cosine

NEGATIVE_STRATEGIES = {"uniform", "popularity_aware", "train_only_hard"}
MAX_POPULARITY_REJECTIONS_PER_OUTPUT = 32


def stable_seed(seed: int, *parts: str) -> int:
    payload = "\0".join((str(seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


@dataclass(frozen=True, slots=True)
class TrainOnlyNegativeSampler:
    train_item_ids: tuple[str, ...]
    popularity: Mapping[str, float]
    title_features: Mapping[str, EncodedTitle]
    alpha: float = 0.75
    _item_to_index: Mapping[str, int] = field(init=False, repr=False)
    _popularity_weights: tuple[float, ...] = field(init=False, repr=False)
    _popularity_cdf: tuple[float, ...] = field(init=False, repr=False)
    _popularity_total: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.train_item_ids:
            raise ValueError("negative sampler requires train items")
        if self.alpha < 0:
            raise ValueError("popularity alpha must be non-negative")
        if len(set(self.train_item_ids)) != len(self.train_item_ids):
            raise ValueError("negative sampler train items must be unique")
        if any(item_id not in self.title_features for item_id in self.train_item_ids):
            raise ValueError("hard-negative title features must cover every train item")
        item_to_index = {item_id: index for index, item_id in enumerate(self.train_item_ids)}
        weights: list[float] = []
        cdf: list[float] = []
        total = 0.0
        for item_id in self.train_item_ids:
            raw = float(self.popularity.get(item_id, 0.0))
            weight = max(0.0, raw) ** self.alpha if math.isfinite(raw) else 0.0
            total += weight
            weights.append(weight)
            cdf.append(total)
        object.__setattr__(self, "_item_to_index", item_to_index)
        object.__setattr__(self, "_popularity_weights", tuple(weights))
        object.__setattr__(self, "_popularity_cdf", tuple(cdf))
        object.__setattr__(self, "_popularity_total", total)

    def _excluded_indices(self, positive_item_id: str, seen_item_ids: set[str]) -> tuple[int, ...]:
        excluded = {
            index
            for item_id in seen_item_ids | {positive_item_id}
            if (index := self._item_to_index.get(item_id)) is not None
        }
        return tuple(sorted(excluded))

    def _uniform_sample(
        self,
        rng: random.Random,
        *,
        excluded_indices: tuple[int, ...],
        count: int,
    ) -> list[str]:
        eligible_count = len(self.train_item_ids) - len(excluded_indices)
        count = min(count, eligible_count)
        if count <= 0:
            return []
        eligible_ranks = rng.sample(range(eligible_count), count)
        indices_by_rank: dict[int, int] = {}
        excluded_offset = 0
        for rank in sorted(eligible_ranks):
            catalog_index = rank + excluded_offset
            while (
                excluded_offset < len(excluded_indices)
                and excluded_indices[excluded_offset] <= catalog_index
            ):
                catalog_index += 1
                excluded_offset += 1
            indices_by_rank[rank] = catalog_index
        return [self.train_item_ids[indices_by_rank[rank]] for rank in eligible_ranks]

    def _popularity_sample(
        self,
        rng: random.Random,
        *,
        excluded_indices: tuple[int, ...],
        count: int,
    ) -> list[str]:
        eligible_count = len(self.train_item_ids) - len(excluded_indices)
        count = min(count, eligible_count)
        if count <= 0:
            return []
        if self._popularity_total <= 0:
            return self._uniform_sample(rng, excluded_indices=excluded_indices, count=count)

        unavailable = set(excluded_indices)
        selected: list[int] = []
        while len(selected) < count:
            accepted = False
            for _attempt in range(MAX_POPULARITY_REJECTIONS_PER_OUTPUT):
                point = rng.random() * self._popularity_total
                index = bisect_right(self._popularity_cdf, point)
                if index >= len(self.train_item_ids) or index in unavailable:
                    continue
                unavailable.add(index)
                selected.append(index)
                accepted = True
                break
            if not accepted:
                break

        remaining = count - len(selected)
        if remaining:
            # Pathological excluded-weight cases get one bounded-memory catalog pass.
            fallback = heapq.nsmallest(
                remaining,
                (index for index in range(len(self.train_item_ids)) if index not in unavailable),
                key=lambda index: (-self._popularity_weights[index], index),
            )
            selected.extend(fallback)
        return [self.train_item_ids[index] for index in selected]

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
        excluded_indices = self._excluded_indices(positive_item_id, seen_item_ids)
        eligible_count = len(self.train_item_ids) - len(excluded_indices)
        if eligible_count <= 0:
            return []
        count = min(count, eligible_count)
        rng = random.Random(stable_seed(seed, user_id, positive_item_id, strategy))
        if strategy == "uniform":
            return self._uniform_sample(rng, excluded_indices=excluded_indices, count=count)
        if strategy == "popularity_aware":
            return self._popularity_sample(rng, excluded_indices=excluded_indices, count=count)
        positive_title = self.title_features[positive_item_id]
        pool_limit = max(256, count * 64)
        random_pool = self._uniform_sample(
            rng,
            excluded_indices=excluded_indices,
            count=min(pool_limit, eligible_count),
        )
        excluded = set(excluded_indices)
        popularity_pool = [
            self.train_item_ids[index]
            for index in heapq.nsmallest(
                min(64, eligible_count),
                (index for index in range(len(self.train_item_ids)) if index not in excluded),
                key=lambda index: (-self._popularity_weights[index], self.train_item_ids[index]),
            )
        ]
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
