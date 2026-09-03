from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import torch

from recsys.models.text import merge_encoded_titles, sparse_cosine
from recsys.serving.runtime import LoadedRecommendationModel

from .domain import RankedCandidate, RecallCandidate
from .ranking import MergedScore, derived_title_topic, min_max

PERSONALIZED_RECALL_SOURCES = frozenset(
    {"dssm", "item_item_cf", "profile_title", "popular", "explore"}
)


def _id_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


@dataclass(frozen=True, slots=True)
class CatalogItem:
    item_id: str
    title: str
    cover: str | None
    likes: int
    views: int
    metadata_status: str = "complete"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    candidates: tuple[RecallCandidate, ...]
    fallback_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RankingResult:
    candidates: tuple[RankedCandidate, ...]
    title_vectors: Mapping[str, tuple[float, ...] | None]
    fallback_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ItemItemIndex:
    """Train-history item-item cosine index with deterministic ties."""

    neighbors: Mapping[str, tuple[tuple[str, float], ...]]
    source_histories: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_histories(cls, histories: Mapping[str, Sequence[str]]) -> ItemItemIndex:
        item_frequency: Counter[str] = Counter()
        pairs: Counter[tuple[str, str]] = Counter()
        for user_id in sorted(histories, key=_id_key):
            unique = sorted(set(histories[user_id]), key=_id_key)
            item_frequency.update(unique)
            for index, left in enumerate(unique):
                for right in unique[index + 1 :]:
                    pairs[(left, right)] += 1
        output: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for (left, right), count in pairs.items():
            score = count / math.sqrt(item_frequency[left] * item_frequency[right])
            output[left].append((right, score))
            output[right].append((left, score))
        return cls(
            MappingProxyType(
                {
                    item_id: tuple(sorted(rows, key=lambda row: (-row[1], _id_key(row[0]))))
                    for item_id, rows in sorted(output.items(), key=lambda row: _id_key(row[0]))
                }
            ),
            MappingProxyType(
                {user_id: tuple(histories[user_id]) for user_id in sorted(histories, key=_id_key)}
            ),
        )

    def recall(self, seed_item_ids: Sequence[str], *, top_n: int) -> list[RecallCandidate]:
        scores: dict[str, float] = {}
        seeds = set(seed_item_ids)
        for seed in sorted(seeds, key=_id_key):
            for item_id, score in self.neighbors.get(seed, ()):
                if item_id not in seeds:
                    scores[item_id] = max(scores.get(item_id, 0.0), float(score))
        return [
            RecallCandidate(
                item_id=item_id,
                source="item_item_cf",
                raw_score=score,
                reason="train-history item-item cosine",
            )
            for item_id, score in sorted(
                scores.items(), key=lambda row: (-row[1], _id_key(row[0]))
            )[:top_n]
        ]


def _stable_unit(seed: int, item_id: str) -> float:
    digest = hashlib.sha256(f"{seed}:{item_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _title_tokens(title: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return list(dict.fromkeys(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)))[:32]


def build_profile_title_preferences(titles: Sequence[str]) -> dict[str, dict[str, int]]:
    """Build the same token/score shape consumed by serving profile-title recall."""

    scores: Counter[str] = Counter()
    for title in titles:
        scores.update(_title_tokens(title))
    return {token: {"score": score} for token, score in sorted(scores.items())}


def _popular_score(item: CatalogItem, train_popularity: Mapping[str, float]) -> float:
    training = math.log1p(max(0.0, float(train_popularity.get(item.item_id, 0.0))))
    engagement = math.log1p(max(0, item.likes)) + 0.25 * math.log1p(max(0, item.views))
    return training + engagement


def retrieve_candidates(
    *,
    feed_type: str,
    catalog: Sequence[CatalogItem],
    bundle: object | None,
    source_user_id: str | None,
    profile_title_preferences: Mapping[str, object],
    recent_item_ids: Sequence[str],
    item_item_index: ItemItemIndex | None,
    seed: int,
    top_n: int,
    enabled_sources: Collection[str] | None = None,
    dssm_recaller: Callable[[str, int], Sequence[tuple[str, float]]] | None = None,
) -> RetrievalResult:
    sources = PERSONALIZED_RECALL_SOURCES if enabled_sources is None else frozenset(enabled_sources)
    unknown_sources = sources - PERSONALIZED_RECALL_SOURCES
    if unknown_sources:
        raise ValueError(f"unknown recall sources: {sorted(unknown_sources)}")
    candidates: list[RecallCandidate] = []
    fallbacks: list[str] = []
    popularity = getattr(bundle, "popularity", {}) if bundle is not None else {}

    if feed_type == "personalized":
        if "dssm" in sources and bundle is not None and source_user_id:
            try:
                recalled = (
                    dssm_recaller(source_user_id, top_n)
                    if dssm_recaller is not None
                    else LoadedRecommendationModel(bundle).recall(source_user_id, top_n=top_n)
                )
                candidates.extend(
                    RecallCandidate(
                        item_id=item_id,
                        source="dssm",
                        raw_score=score,
                        reason="active ModelBundle DSSM recall",
                    )
                    for item_id, score in recalled
                )
            except Exception as exc:
                fallbacks.append(f"dssm_recall_failed:{type(exc).__name__}")
        elif "dssm" in sources:
            fallbacks.append("cold_user_or_model_unavailable")

        if "item_item_cf" in sources and item_item_index is not None and recent_item_ids:
            candidates.extend(item_item_index.recall(recent_item_ids, top_n=top_n))
        elif "item_item_cf" in sources and recent_item_ids:
            fallbacks.append("item_item_index_unavailable")

        preference_scores = {
            str(token): int(value.get("score", 0))
            for token, value in profile_title_preferences.items()
            if isinstance(token, str) and isinstance(value, dict)
        }
        if "profile_title" in sources and any(score > 0 for score in preference_scores.values()):
            profile_rows: list[RecallCandidate] = []
            for item in catalog:
                overlap = sum(
                    max(0, preference_scores.get(token, 0)) for token in _title_tokens(item.title)
                )
                if overlap > 0:
                    profile_rows.append(
                        RecallCandidate(
                            item_id=item.item_id,
                            source="profile_title",
                            raw_score=float(overlap),
                            reason="positive profile/title token overlap",
                        )
                    )
            profile_rows.sort(key=lambda row: (-row.raw_score, row.item_id))
            candidates.extend(profile_rows[:top_n])

    include_popular = "popular" in sources and feed_type in {"personalized", "popular"}
    include_explore = "explore" in sources and feed_type in {"personalized", "explore"}
    popular: list[RecallCandidate] = []
    if include_popular or include_explore:
        popular = sorted(
            (
                RecallCandidate(
                    item_id=item.item_id,
                    source="popular",
                    raw_score=_popular_score(item, popularity),
                    reason="train and online engagement popularity",
                )
                for item in catalog
            ),
            key=lambda row: (-row.raw_score, row.item_id),
        )[:top_n]
    if include_popular:
        candidates.extend(popular)

    if include_explore:
        maximum_popularity = max((row.raw_score for row in popular), default=0.0)
        explore = [
            RecallCandidate(
                item_id=item.item_id,
                source="explore",
                raw_score=(
                    _stable_unit(seed, item.item_id)
                    + 0.25 * (1.0 - (_popular_score(item, popularity) / maximum_popularity))
                    if maximum_popularity > 0
                    else _stable_unit(seed, item.item_id)
                ),
                reason="deterministic low-popularity exploration",
            )
            for item in catalog
        ]
        explore.sort(key=lambda row: (-row.raw_score, row.item_id))
        candidates.extend(explore[:top_n])

    if not candidates:
        fallbacks.append("empty_retrieval")
    return RetrievalResult(tuple(candidates), tuple(fallbacks))


def _dense_title_vector(encoded: object, dimension: int) -> tuple[float, ...]:
    vector = [0.0] * dimension
    for token_id, weight in zip(encoded.token_ids, encoded.weights, strict=True):
        if 0 <= int(token_id) < dimension:
            vector[int(token_id)] = float(weight)
    return tuple(vector)


def rank_candidates(
    *,
    merged: Sequence[MergedScore],
    catalog: Mapping[str, CatalogItem],
    bundle: object | None,
    source_user_id: str | None,
    positive_history_titles: Sequence[str],
    profile_activity_count: int,
) -> RankingResult:
    title_vectors: dict[str, tuple[float, ...] | None] = {}
    fallbacks: list[str] = []
    encoder = getattr(bundle, "title_encoder", None) if bundle is not None else None
    history_title = None
    if encoder is not None and positive_history_titles:
        history_title = merge_encoded_titles(
            encoder.transform(title) for title in positive_history_titles
        )

    interim: list[RankedCandidate] = []
    for row in merged:
        item = catalog.get(row.item_id)
        if item is None:
            continue
        encoded = encoder.transform(item.title) if encoder is not None else None
        token_weights = (
            dict(zip(encoded.token_ids, encoded.weights, strict=True))
            if encoded is not None
            else None
        )
        title_vectors[item.item_id] = (
            _dense_title_vector(encoded, encoder.bucket_count + 1) if encoded is not None else None
        )
        interim.append(
            RankedCandidate(
                item_id=item.item_id,
                title=item.title,
                cover=item.cover,
                source=row.source,
                sources=row.sources,
                raw_score=row.raw_score,
                normalized_score=row.normalized_score,
                score=row.normalized_score,
                reason=row.reason,
                original_rank=row.original_rank,
                title_topic=derived_title_topic(item.title, encoded_token_weights=token_weights),
            )
        )

    user_to_index = getattr(bundle, "user_to_index", {}) if bundle is not None else {}
    item_to_index = getattr(bundle, "item_to_index", {}) if bundle is not None else {}
    user_index = user_to_index.get(source_user_id) if source_user_id else None
    eligible = [candidate for candidate in interim if candidate.item_id in item_to_index]
    if bundle is None or user_index is None or not eligible:
        fallbacks.append("deepfm_user_or_model_unavailable")
        return RankingResult(
            tuple(sorted(interim, key=lambda row: (-row.score, row.original_rank, row.item_id))),
            title_vectors,
            tuple(fallbacks),
        )

    try:
        maximum_popularity = (
            max(
                (math.log1p(max(0.0, float(value))) for value in bundle.popularity.values()),
                default=1.0,
            )
            or 1.0
        )
        dense_rows: list[list[float]] = []
        merged_by_item = {row.item_id: row for row in merged}
        for candidate in eligible:
            encoded = encoder.transform(candidate.title) if encoder is not None else None
            title_similarity = (
                sparse_cosine(history_title, encoded)
                if history_title is not None and encoded is not None
                else 0.0
            )
            popularity = (
                math.log1p(max(0.0, float(bundle.popularity.get(candidate.item_id, 0.0))))
                / maximum_popularity
            )
            dense_rows.append(
                [
                    merged_by_item[candidate.item_id].dssm_recall_score,
                    title_similarity,
                    popularity,
                    1.0 - popularity,
                    min(1.0, math.log1p(profile_activity_count) / math.log1p(100)),
                    1.0,
                ]
            )
        bundle.deepfm.eval()
        with torch.no_grad():
            scores = bundle.deepfm(
                torch.tensor([user_index] * len(eligible), dtype=torch.long),
                torch.tensor([item_to_index[row.item_id] for row in eligible], dtype=torch.long),
                torch.zeros(len(eligible), dtype=torch.long),
                torch.tensor(dense_rows, dtype=torch.float32),
            ).tolist()
        by_item = {
            candidate.item_id: float(score)
            for candidate, score in zip(eligible, scores, strict=True)
        }
        ranked = [
            RankedCandidate(
                item_id=row.item_id,
                title=row.title,
                cover=row.cover,
                source=row.source,
                sources=row.sources,
                raw_score=row.raw_score,
                normalized_score=row.normalized_score,
                score=by_item.get(row.item_id, row.score),
                reason=(
                    row.reason + "; active ModelBundle DeepFM rank"
                    if row.item_id in by_item
                    else row.reason + "; DeepFM item unavailable, merged-score fallback"
                ),
                original_rank=row.original_rank,
                title_topic=row.title_topic,
            )
            for row in interim
        ]
    except Exception as exc:
        fallbacks.append(f"deepfm_rank_failed:{type(exc).__name__}")
        ranked = interim
    ranked.sort(key=lambda row: (-row.score, row.original_rank, row.item_id))
    normalized_scores = min_max([row.score for row in ranked])
    ranked = [
        RankedCandidate(
            item_id=row.item_id,
            title=row.title,
            cover=row.cover,
            source=row.source,
            sources=row.sources,
            raw_score=row.raw_score,
            normalized_score=normalized_scores[index],
            score=row.score,
            reason=row.reason,
            original_rank=index,
            title_topic=row.title_topic,
        )
        for index, row in enumerate(ranked)
    ]
    return RankingResult(tuple(ranked), title_vectors, tuple(fallbacks))
