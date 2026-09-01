from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Mapping


def _stable_candidates(all_items: Iterable[str], train_seen: set[str]) -> list[str]:
    return sorted(set(all_items).difference(train_seen), key=lambda value: (len(value), value))


def uniform_sample(
    all_items: Iterable[str], train_seen: set[str], count: int, *, seed: int
) -> list[str]:
    """Sample without replacement, excluding train history only."""

    candidates = _stable_candidates(all_items, train_seen)
    if count >= len(candidates):
        shuffled = candidates.copy()
        random.Random(seed).shuffle(shuffled)
        return shuffled
    return random.Random(seed).sample(candidates, count)


def popularity_aware_sample(
    all_items: Iterable[str],
    train_seen: set[str],
    train_popularity: Mapping[str, int | float],
    count: int,
    *,
    seed: int,
    alpha: float = 0.75,
) -> list[str]:
    """Weighted sample without replacement using train-only popularity."""

    if alpha < 0:
        raise ValueError("alpha must be non-negative")
    candidates = _stable_candidates(all_items, train_seen)
    if not candidates or count <= 0:
        return []
    rng = random.Random(seed)
    selected: list[str] = []
    remaining = candidates.copy()
    while remaining and len(selected) < count:
        weights = [
            max(float(train_popularity.get(item_id, 0)), 0.0) ** alpha for item_id in remaining
        ]
        if sum(weights) == 0:
            index = rng.randrange(len(remaining))
        else:
            point = rng.random() * sum(weights)
            cumulative = 0.0
            index = len(remaining) - 1
            for candidate_index, weight in enumerate(weights):
                cumulative += weight
                if point < cumulative:
                    index = candidate_index
                    break
        selected.append(remaining.pop(index))
    return selected


def sampling_seed(global_seed: int, user_id: str, strategy: str) -> int:
    payload = f"{global_seed}\0{user_id}\0{strategy}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
