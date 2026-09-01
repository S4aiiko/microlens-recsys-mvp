from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

EVENT_TYPES = (
    "impression",
    "click",
    "like",
    "not_interested",
    "dwell",
    "revisit",
    "share",
)
FEED_TYPES = ("personalized", "popular", "explore")


class AnalyticsContractError(ValueError):
    """The database snapshot or immutable export violates the frozen contract."""


def require_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AnalyticsContractError(f"{field} must be timezone-aware UTC")
    normalized = value.astimezone(UTC)
    if normalized.utcoffset() != UTC.utcoffset(normalized):
        raise AnalyticsContractError(f"{field} must normalize to UTC")
    return normalized


def enum_value(value: str | enum.Enum) -> str:
    raw = value.value if isinstance(value, enum.Enum) else value
    return str(raw)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    from_utc: datetime
    to_utc: datetime

    def __post_init__(self) -> None:
        start = require_utc(self.from_utc, "from_utc")
        end = require_utc(self.to_utc, "to_utc")
        if start >= end:
            raise AnalyticsContractError("analytics window must satisfy from_utc < to_utc")
        object.__setattr__(self, "from_utc", start)
        object.__setattr__(self, "to_utc", end)


@dataclass(frozen=True, slots=True)
class EventRow:
    event_sequence_id: int
    event_id: str
    exposure_id: str
    request_id: str
    user_id: str
    item_id: str
    position: int
    feed_type: str
    source: str
    event_type: str
    client_timestamp: datetime | None
    server_timestamp: datetime
    duration_ms: int | None
    payload_json: str

    def __post_init__(self) -> None:
        _positive_sequence(self.event_sequence_id, "event_sequence_id")
        for field in ("event_id", "exposure_id", "request_id", "user_id", "item_id"):
            _bounded_text(getattr(self, field), field, maximum=255)
        _nonnegative_integer(self.position, "position")
        if self.feed_type not in FEED_TYPES:
            raise AnalyticsContractError("unknown event feed_type")
        if self.event_type not in EVENT_TYPES:
            raise AnalyticsContractError("unknown event_type")
        _bounded_text(self.source, "source", maximum=128)
        if self.client_timestamp is not None:
            require_utc(self.client_timestamp, "client_timestamp")
        require_utc(self.server_timestamp, "server_timestamp")
        if self.duration_ms is not None:
            _nonnegative_integer(self.duration_ms, "duration_ms")
            if self.duration_ms > 86_400_000:
                raise AnalyticsContractError("duration_ms exceeds one day")
        if self.event_type == "dwell" and self.duration_ms is None:
            raise AnalyticsContractError("dwell event requires duration_ms")
        if not isinstance(self.payload_json, str):
            raise AnalyticsContractError("payload_json must be text")


@dataclass(frozen=True, slots=True)
class ExposureRow:
    canonical_event_sequence_id: int
    exposure_id: str
    request_id: str
    snapshot_id: str
    user_id: str
    item_id: str
    position: int
    feed_type: str
    source: str
    model_version: str
    exposed_at: datetime

    def __post_init__(self) -> None:
        _positive_sequence(self.canonical_event_sequence_id, "canonical_event_sequence_id")
        for field in (
            "exposure_id",
            "request_id",
            "snapshot_id",
            "user_id",
            "item_id",
        ):
            _bounded_text(getattr(self, field), field, maximum=255)
        _nonnegative_integer(self.position, "position")
        if self.feed_type not in FEED_TYPES:
            raise AnalyticsContractError("unknown exposure feed_type")
        _bounded_text(self.source, "source", maximum=128)
        _bounded_text(self.model_version, "model_version", maximum=255)
        require_utc(self.exposed_at, "exposed_at")


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    window: TimeWindow
    previous_event_sequence_exclusive: int
    event_sequence_cutoff_inclusive: int
    events: tuple[EventRow, ...]
    exposures: tuple[ExposureRow, ...]
    postgres_event_counts: dict[str, int]
    postgres_exposure_counts: dict[str, int]


class AnalyticsSource(Protocol):
    """Collect one PostgreSQL-authoritative bounded snapshot.

    Implementations must choose the maximum event sequence before reading data and
    bind every row/count query to ``(previous, cutoff]`` and the same UTC window.
    """

    def collect(
        self, window: TimeWindow, *, previous_event_sequence_exclusive: int
    ) -> AnalyticsSnapshot: ...


def _bounded_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AnalyticsContractError(f"{field} must contain 1..{maximum} characters")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalyticsContractError(f"{field} must be a non-negative integer")
    return value


def _positive_sequence(value: object, field: str) -> int:
    result = _nonnegative_integer(value, field)
    if result < 1:
        raise AnalyticsContractError(f"{field} must be positive")
    return result
