from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any


def finite(value: float, *, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class RecallCandidate:
    item_id: str
    source: str
    raw_score: float
    reason: str

    def __post_init__(self) -> None:
        if not self.item_id or not self.source or not self.reason:
            raise ValueError("recall candidate text fields must be non-empty")
        finite(self.raw_score, label="raw_score")

    def as_dict(self) -> dict[str, str | float]:
        return {
            "item_id": self.item_id,
            "source": self.source,
            "raw_score": self.raw_score,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RecallCandidate:
        if not isinstance(value, dict) or set(value) != {
            "item_id",
            "source",
            "raw_score",
            "reason",
        }:
            raise ValueError("cached recall candidate has an invalid shape")
        if any(
            not isinstance(value[field], str) or not value[field]
            for field in ("item_id", "source", "reason")
        ):
            raise ValueError("cached recall candidate text fields are invalid")
        raw_score = value["raw_score"]
        if isinstance(raw_score, bool) or not isinstance(raw_score, int | float):
            raise ValueError("cached recall candidate score is invalid")
        return cls(
            item_id=value["item_id"],
            source=value["source"],
            raw_score=finite(float(raw_score), label="cached raw_score"),
            reason=value["reason"],
        )


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    item_id: str
    title: str
    cover: str | None
    source: str
    sources: tuple[str, ...]
    raw_score: float
    normalized_score: float
    score: float
    reason: str
    original_rank: int
    title_topic: str
    promotion_rule_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.original_rank < 0:
            raise ValueError("original_rank must be nonnegative")
        finite(self.raw_score, label="raw_score")
        finite(self.normalized_score, label="normalized_score")
        finite(self.score, label="score")


@dataclass(frozen=True, slots=True)
class MMRStep:
    item_id: str
    selection_index: int
    normalized_relevance: float
    max_similarity: float
    mmr_score: float
    fallback_reason: str | None = None


@dataclass(slots=True)
class RecommendationTrace:
    snapshot_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    feed_type: str = ""
    model_version: str = ""
    source_counts: dict[str, int] = field(default_factory=dict)
    filter_counts: dict[str, int] = field(default_factory=dict)
    cache_status: str = "not_used"
    latency_ms: int = 0
    fallback_reasons: list[str] = field(default_factory=list)
    mmr_steps: list[MMRStep] = field(default_factory=list)

    def add_fallback(self, reason: str) -> None:
        if reason and reason not in self.fallback_reasons:
            self.fallback_reasons.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "request_id": str(self.request_id) if self.request_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "feed_type": self.feed_type,
            "model_version": self.model_version,
            "source_counts": dict(sorted(self.source_counts.items())),
            "filter_counts": dict(sorted(self.filter_counts.items())),
            "cache_status": self.cache_status,
            "latency_ms": self.latency_ms,
            "fallback_reasons": self.fallback_reasons,
            "mmr_steps": [
                {
                    "item_id": step.item_id,
                    "selection_index": step.selection_index,
                    "normalized_relevance": step.normalized_relevance,
                    "max_similarity": step.max_similarity,
                    "mmr_score": step.mmr_score,
                    "fallback_reason": step.fallback_reason,
                }
                for step in self.mmr_steps
            ],
        }
