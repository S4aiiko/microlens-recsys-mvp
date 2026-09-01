from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

from apps.api.app.async_runtime.domain import require_aware


class AlertStatus(StrEnum):
    FIRING = "firing"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class Comparator(StrEnum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class Aggregation(StrEnum):
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"


@dataclass(frozen=True)
class AlertRule:
    rule_id: uuid.UUID
    name: str
    metric_name: str
    comparator: Comparator
    threshold: float
    window_seconds: int
    min_samples: int = 1
    aggregation: Aggregation = Aggregation.AVG
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValueError("alert name must contain 1..128 characters")
        if not self.metric_name or len(self.metric_name) > 128:
            raise ValueError("metric_name must contain 1..128 characters")
        if isinstance(self.threshold, bool) or not isfinite(float(self.threshold)):
            raise ValueError("threshold must be finite")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.min_samples <= 0:
            raise ValueError("min_samples must be positive")


@dataclass(frozen=True)
class MetricObservation:
    metric_name: str
    value: float
    sample_count: int
    window_start: datetime
    window_end: datetime

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isfinite(float(self.value)):
            raise ValueError("metric value must be finite")
        if self.sample_count < 0:
            raise ValueError("sample_count must be nonnegative")
        require_aware(self.window_start, field="window_start")
        require_aware(self.window_end, field="window_end")
        if self.window_end <= self.window_start:
            raise ValueError("metric window must be non-empty")


@dataclass(frozen=True)
class AlertOccurrence:
    occurrence_id: uuid.UUID
    rule_id: uuid.UUID
    status: AlertStatus
    observed_value: float
    sample_count: int
    window_start: datetime
    window_end: datetime
    fired_at: datetime
    version: int
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None
    resolve_reason: str | None = None


@dataclass(frozen=True)
class AlertEvaluation:
    rule_id: uuid.UUID
    condition_met: bool
    transition: str
    occurrence: AlertOccurrence | None


def compare(comparator: Comparator, value: float, threshold: float) -> bool:
    if comparator == Comparator.GT:
        return value > threshold
    if comparator == Comparator.GTE:
        return value >= threshold
    if comparator == Comparator.LT:
        return value < threshold
    return value <= threshold
