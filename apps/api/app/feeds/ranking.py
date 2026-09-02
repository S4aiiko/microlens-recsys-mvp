from __future__ import annotations

import hashlib
import math
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .domain import MMRStep, RankedCandidate, RecallCandidate


@dataclass(frozen=True, slots=True)
class MergedScore:
    item_id: str
    source: str
    sources: tuple[str, ...]
    raw_score: float
    dssm_recall_score: float
    normalized_score: float
    reason: str
    original_rank: int


@dataclass(frozen=True, slots=True)
class PromotionPlacement:
    rule_id: uuid.UUID
    item_id: str
    priority: int
    target_position: int | None
    reason: str


def min_max(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("normalization values must be finite")
    minimum = min(values)
    maximum = max(values)
    if maximum == minimum:
        return [1.0] * len(values)
    scale = maximum - minimum
    return [(float(value) - minimum) / scale for value in values]


def merge_recall(candidates: Sequence[RecallCandidate]) -> list[MergedScore]:
    """Normalize within each source, then deterministically merge duplicate items."""

    by_source: dict[str, list[RecallCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_source[candidate.source].append(candidate)

    normalized_rows: list[tuple[RecallCandidate, float]] = []
    for source in sorted(by_source):
        rows = sorted(by_source[source], key=lambda row: (-row.raw_score, row.item_id))
        normalized_rows.extend(zip(rows, min_max([row.raw_score for row in rows]), strict=True))

    by_item: dict[str, list[tuple[RecallCandidate, float]]] = defaultdict(list)
    for row, normalized in normalized_rows:
        by_item[row.item_id].append((row, normalized))

    merged: list[MergedScore] = []
    for item_id, rows in by_item.items():
        ordered = sorted(rows, key=lambda entry: (-entry[1], -entry[0].raw_score, entry[0].source))
        primary, primary_normalized = ordered[0]
        sources = tuple(sorted({row.source for row, _normalized in rows}))
        # A small, bounded agreement bonus lets independent recall sources matter without
        # allowing source count to swamp the strongest normalized signal.
        combined = min(1.0, primary_normalized + 0.05 * (len(sources) - 1))
        reasons = "; ".join(
            f"{row.source}:{row.reason}"
            for row, _normalized in sorted(rows, key=lambda x: x[0].source)
        )
        merged.append(
            MergedScore(
                item_id=item_id,
                source=primary.source,
                sources=sources,
                raw_score=primary.raw_score,
                dssm_recall_score=max(
                    (row.raw_score for row, _normalized in rows if row.source == "dssm"),
                    default=0.0,
                ),
                normalized_score=combined,
                reason=reasons,
                original_rank=0,
            )
        )
    merged.sort(key=lambda row: (-row.normalized_score, -row.raw_score, row.item_id))
    return [replace(row, original_rank=index) for index, row in enumerate(merged)]


def derived_title_topic(
    title: str,
    *,
    encoded_token_weights: Mapping[int, float] | None = None,
) -> str:
    """Return an explicitly derived group, never an author/tag claim."""

    if encoded_token_weights:
        token = min(
            encoded_token_weights,
            key=lambda key: (-float(encoded_token_weights[key]), int(key)),
        )
        return f"derived_title_topic:{token}"
    digest = hashlib.sha256(title.casefold().strip().encode("utf-8")).hexdigest()[:12]
    return f"derived_title_hash:{digest}"


def topic_deduplicate(
    candidates: Sequence[RankedCandidate], *, max_per_topic: int = 1
) -> tuple[list[RankedCandidate], list[RankedCandidate]]:
    if max_per_topic < 1:
        raise ValueError("max_per_topic must be positive")
    counts: dict[str, int] = defaultdict(int)
    kept: list[RankedCandidate] = []
    removed: list[RankedCandidate] = []
    for candidate in candidates:
        if counts[candidate.title_topic] >= max_per_topic:
            removed.append(candidate)
            continue
        counts[candidate.title_topic] += 1
        kept.append(candidate)
    return kept, removed


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("MMR vectors must have the same non-zero dimension")
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, dot / (left_norm * right_norm))


def mmr_rank(
    candidates: Sequence[RankedCandidate],
    *,
    vectors: Mapping[str, Sequence[float] | None],
    lambda_value: float = 0.75,
) -> tuple[list[RankedCandidate], list[MMRStep]]:
    """Apply the exact frozen section 5.8 deterministic MMR contract."""

    if not 0.0 <= lambda_value <= 1.0:
        raise ValueError("MMR lambda must be between zero and one")
    if not candidates:
        return [], []
    normalized = dict(
        zip(
            (candidate.item_id for candidate in candidates),
            min_max([candidate.score for candidate in candidates]),
            strict=True,
        )
    )
    remaining = list(candidates)
    selected: list[RankedCandidate] = []
    steps: list[MMRStep] = []
    while remaining:
        evaluated: list[tuple[float, int, str, RankedCandidate, float, str | None]] = []
        for candidate in remaining:
            vector = vectors.get(candidate.item_id)
            fallback_reason: str | None = None
            if vector is None:
                max_similarity = 0.0
                mmr_score = candidate.score
                fallback_reason = "missing_title_vector_original_score"
            else:
                similarities = [
                    _cosine(vector, selected_vector)
                    for chosen in selected
                    if (selected_vector := vectors.get(chosen.item_id)) is not None
                ]
                max_similarity = max(similarities, default=0.0)
                mmr_score = (
                    lambda_value * normalized[candidate.item_id]
                    - (1.0 - lambda_value) * max_similarity
                )
            evaluated.append(
                (
                    mmr_score,
                    candidate.original_rank,
                    candidate.item_id,
                    candidate,
                    max_similarity,
                    fallback_reason,
                )
            )
        _score, _rank, _item_id, chosen, similarity, fallback_reason = min(
            evaluated, key=lambda row: (-row[0], row[1], row[2])
        )
        selected.append(chosen)
        remaining.remove(chosen)
        steps.append(
            MMRStep(
                item_id=chosen.item_id,
                selection_index=len(selected) - 1,
                normalized_relevance=normalized[chosen.item_id],
                max_similarity=similarity,
                mmr_score=_score,
                fallback_reason=fallback_reason,
            )
        )
    return selected, steps


def apply_promotions(
    natural: Sequence[RankedCandidate],
    *,
    placements: Sequence[PromotionPlacement],
    promoted_candidates: Mapping[str, RankedCandidate],
) -> list[RankedCandidate]:
    """Insert active online promotions after natural diversity ordering."""

    selected_rules: list[PromotionPlacement] = []
    seen_items: set[str] = set()
    for placement in sorted(
        placements,
        key=lambda row: (
            -row.priority,
            row.target_position is None,
            row.target_position or 0,
            str(row.rule_id),
        ),
    ):
        if placement.item_id in seen_items or placement.item_id not in promoted_candidates:
            continue
        seen_items.add(placement.item_id)
        selected_rules.append(placement)

    natural_rows = [candidate for candidate in natural if candidate.item_id not in seen_items]
    slots: dict[int, RankedCandidate] = {}
    for placement in (row for row in selected_rules if row.target_position is not None):
        slot = int(placement.target_position or 0)
        while slot in slots:
            slot += 1
        slots[slot] = replace(
            promoted_candidates[placement.item_id],
            source="promotion",
            sources=("promotion",),
            reason=f"promotion:{placement.reason}",
            promotion_rule_id=placement.rule_id,
        )

    output: list[RankedCandidate] = []
    natural_index = 0
    position = 0
    while natural_index < len(natural_rows) or slots:
        if position in slots:
            output.append(slots.pop(position))
        elif natural_index < len(natural_rows):
            output.append(natural_rows[natural_index])
            natural_index += 1
        elif slots:
            position = min(slots)
            continue
        position += 1
    for placement in selected_rules:
        if placement.target_position is not None:
            continue
        output.append(
            replace(
                promoted_candidates[placement.item_id],
                source="promotion",
                sources=("promotion",),
                reason=f"promotion:{placement.reason}",
                promotion_rule_id=placement.rule_id,
            )
        )
    return output
