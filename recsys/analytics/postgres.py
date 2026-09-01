from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

from .contracts import (
    EVENT_TYPES,
    FEED_TYPES,
    AnalyticsContractError,
    AnalyticsSnapshot,
    EventRow,
    ExposureRow,
    TimeWindow,
    enum_value,
)


class PostgreSQLAnalyticsSource:
    """SQLAlchemy adapter for a bounded PostgreSQL analytics snapshot.

    The maximum ``events.id`` is captured before all data and aggregate queries.
    Every query is then bounded to ``(previous, cutoff]``. This preserves a stable
    source boundary even if the caller's transaction uses READ COMMITTED.
    """

    def __init__(self, session) -> None:
        self.session = session

    @staticmethod
    def _database_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        # PostgreSQL returns aware timestamptz values. SQLite unit fixtures lose the
        # offset on round-trip, so the test-only dialect is normalized as stored UTC.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def collect(
        self, window: TimeWindow, *, previous_event_sequence_exclusive: int
    ) -> AnalyticsSnapshot:
        if (
            isinstance(previous_event_sequence_exclusive, bool)
            or not isinstance(previous_event_sequence_exclusive, int)
            or previous_event_sequence_exclusive < 0
        ):
            raise AnalyticsContractError("previous event watermark must be non-negative")
        from sqlalchemy import func, select

        from apps.api.app.db.models import (
            Event,
            EventType,
            Exposure,
            RecommendationSnapshot,
        )

        cutoff = self.session.scalar(select(func.max(Event.id)))
        cutoff = previous_event_sequence_exclusive if cutoff is None else int(cutoff)
        if cutoff < previous_event_sequence_exclusive:
            raise AnalyticsContractError("PostgreSQL event sequence regressed below watermark")
        event_predicates = (
            Event.id > previous_event_sequence_exclusive,
            Event.id <= cutoff,
            Event.server_timestamp >= window.from_utc,
            Event.server_timestamp < window.to_utc,
        )
        event_models = self.session.scalars(
            select(Event).where(*event_predicates).order_by(Event.id, Event.event_id)
        ).all()
        event_count_rows = self.session.execute(
            select(Event.event_type, func.count(Event.id))
            .where(*event_predicates)
            .group_by(Event.event_type)
        ).all()
        impression = Event
        exposure_rows = self.session.execute(
            select(Exposure, RecommendationSnapshot.feed_type, impression.id)
            .join(
                RecommendationSnapshot,
                RecommendationSnapshot.snapshot_id == Exposure.snapshot_id,
            )
            .join(
                impression,
                (impression.exposure_id == Exposure.id)
                & (impression.event_type == EventType.IMPRESSION),
            )
            .where(
                impression.id > previous_event_sequence_exclusive,
                impression.id <= cutoff,
                Exposure.exposed_at >= window.from_utc,
                Exposure.exposed_at < window.to_utc,
            )
            .order_by(impression.id, Exposure.id)
        ).all()
        exposure_count_rows = self.session.execute(
            select(RecommendationSnapshot.feed_type, func.count(Exposure.id))
            .join(
                RecommendationSnapshot,
                RecommendationSnapshot.snapshot_id == Exposure.snapshot_id,
            )
            .join(
                impression,
                (impression.exposure_id == Exposure.id)
                & (impression.event_type == EventType.IMPRESSION),
            )
            .where(
                impression.id > previous_event_sequence_exclusive,
                impression.id <= cutoff,
                Exposure.exposed_at >= window.from_utc,
                Exposure.exposed_at < window.to_utc,
            )
            .group_by(RecommendationSnapshot.feed_type)
        ).all()
        events = tuple(
            EventRow(
                event_sequence_id=int(event.id),
                event_id=str(event.event_id),
                exposure_id=str(event.exposure_id),
                request_id=str(event.request_id),
                user_id=str(event.user_id),
                item_id=event.item_id,
                position=event.position,
                feed_type=enum_value(event.feed_type),
                source=event.source,
                event_type=enum_value(event.event_type),
                client_timestamp=self._database_utc(event.client_timestamp),
                server_timestamp=self._database_utc(event.server_timestamp),
                duration_ms=event.duration_ms,
                payload_json=json.dumps(
                    event.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ),
            )
            for event in event_models
        )
        exposures = tuple(
            ExposureRow(
                canonical_event_sequence_id=int(canonical_sequence),
                exposure_id=str(exposure.id),
                request_id=str(exposure.request_id),
                snapshot_id=str(exposure.snapshot_id),
                user_id=str(exposure.user_id),
                item_id=exposure.item_id,
                position=exposure.position,
                feed_type=enum_value(feed_type),
                source=exposure.source,
                model_version=exposure.model_version,
                exposed_at=self._database_utc(exposure.exposed_at),
            )
            for exposure, feed_type, canonical_sequence in exposure_rows
        )
        event_counts = Counter(
            {enum_value(event_type): int(count) for event_type, count in event_count_rows}
        )
        exposure_counts = Counter(
            {enum_value(feed_type): int(count) for feed_type, count in exposure_count_rows}
        )
        return AnalyticsSnapshot(
            window=window,
            previous_event_sequence_exclusive=previous_event_sequence_exclusive,
            event_sequence_cutoff_inclusive=cutoff,
            events=events,
            exposures=exposures,
            postgres_event_counts={kind: event_counts[kind] for kind in EVENT_TYPES},
            postgres_exposure_counts={kind: exposure_counts[kind] for kind in FEED_TYPES},
        )
