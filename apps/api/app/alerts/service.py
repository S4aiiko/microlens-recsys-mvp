from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.async_runtime.domain import require_aware
from apps.api.app.db.base import ensure_utc

from .domain import (
    Aggregation,
    AlertEvaluation,
    AlertOccurrence,
    AlertRule,
    AlertStatus,
    Comparator,
    MetricObservation,
    compare,
)
from .tables import AlertOccurrenceRow, AlertRuleRow


class MetricReader(Protocol):
    def observe(
        self,
        metric_name: str,
        *,
        aggregation: Aggregation,
        window_start: datetime,
        window_end: datetime,
    ) -> MetricObservation: ...


class MetricSampleSource(Protocol):
    def samples(
        self, metric_name: str, *, window_start: datetime, window_end: datetime
    ) -> Sequence[float]: ...


class WindowedMetricReader:
    """Aggregate actual source samples; no hard-coded alert values are accepted."""

    def __init__(self, source: MetricSampleSource) -> None:
        self.source = source

    def observe(
        self,
        metric_name: str,
        *,
        aggregation: Aggregation,
        window_start: datetime,
        window_end: datetime,
    ) -> MetricObservation:
        start = require_aware(window_start, field="window_start")
        end = require_aware(window_end, field="window_end")
        if end <= start:
            raise ValueError("metric window must be non-empty")
        raw_values = self.source.samples(metric_name, window_start=start, window_end=end)
        if any(isinstance(value, bool) for value in raw_values):
            raise ValueError("metric samples must be finite numbers")
        values = [float(value) for value in raw_values]
        if any(not isfinite(value) for value in values):
            raise ValueError("metric samples must be finite numbers")
        if aggregation == Aggregation.COUNT:
            value = float(len(values))
        elif not values:
            value = 0.0
        elif aggregation == Aggregation.SUM:
            value = sum(values)
        elif aggregation == Aggregation.MIN:
            value = min(values)
        elif aggregation == Aggregation.MAX:
            value = max(values)
        else:
            value = sum(values) / len(values)
        return MetricObservation(
            metric_name=metric_name,
            value=value,
            sample_count=len(values),
            window_start=start,
            window_end=end,
        )


class SqlAlchemyAlertRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def add_rule(self, rule: AlertRule, *, now: datetime) -> AlertRule:
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            existing = session.get(AlertRuleRow, rule.rule_id)
            if existing is not None:
                if self._rule_view(existing) != rule:
                    raise ValueError("rule_id already has different content")
                return rule
            session.add(
                AlertRuleRow(
                    rule_id=rule.rule_id,
                    name=rule.name,
                    metric_name=rule.metric_name,
                    comparator=rule.comparator.value,
                    threshold=rule.threshold,
                    window_seconds=rule.window_seconds,
                    min_samples=rule.min_samples,
                    aggregation=rule.aggregation.value,
                    enabled=rule.enabled,
                    created_at=event_time,
                )
            )
        return rule

    def get_rule(self, rule_id: uuid.UUID) -> AlertRule:
        with self.session_factory() as session:
            row = session.get(AlertRuleRow, rule_id)
            if row is None:
                raise LookupError("alert rule does not exist")
            return self._rule_view(row)

    def apply_observation(
        self,
        rule: AlertRule,
        observation: MetricObservation,
        *,
        now: datetime,
    ) -> AlertEvaluation:
        event_time = require_aware(now, field="now")
        enough_samples = observation.sample_count >= rule.min_samples
        condition_met = enough_samples and compare(
            rule.comparator, observation.value, rule.threshold
        )
        with self.session_factory.begin() as session:
            persisted_rule = session.get(AlertRuleRow, rule.rule_id, with_for_update=True)
            if persisted_rule is None:
                raise LookupError("alert rule does not exist")
            if not persisted_rule.enabled:
                return AlertEvaluation(rule.rule_id, False, "disabled", None)
            open_row = session.scalar(
                select(AlertOccurrenceRow)
                .where(
                    AlertOccurrenceRow.rule_id == rule.rule_id,
                    AlertOccurrenceRow.status.in_(
                        [AlertStatus.FIRING.value, AlertStatus.ACKNOWLEDGED.value]
                    ),
                )
                .order_by(AlertOccurrenceRow.fired_at.desc())
                .with_for_update()
                .limit(1)
            )
            if condition_met and open_row is None:
                open_row = AlertOccurrenceRow(
                    occurrence_id=uuid.uuid4(),
                    rule_id=rule.rule_id,
                    status=AlertStatus.FIRING.value,
                    observed_value=observation.value,
                    sample_count=observation.sample_count,
                    window_start=observation.window_start,
                    window_end=observation.window_end,
                    fired_at=event_time,
                    version=1,
                )
                session.add(open_row)
                session.flush()
                return AlertEvaluation(rule.rule_id, True, "fired", self._occurrence_view(open_row))
            if condition_met and open_row is not None:
                open_row.observed_value = observation.value
                open_row.sample_count = observation.sample_count
                open_row.window_start = observation.window_start
                open_row.window_end = observation.window_end
                open_row.version += 1
                return AlertEvaluation(
                    rule.rule_id, True, "still_firing", self._occurrence_view(open_row)
                )
            if open_row is not None:
                open_row.observed_value = observation.value
                open_row.sample_count = observation.sample_count
                open_row.window_start = observation.window_start
                open_row.window_end = observation.window_end
                open_row.status = AlertStatus.RESOLVED.value
                open_row.resolved_at = event_time
                open_row.resolve_reason = (
                    "insufficient_samples" if not enough_samples else "threshold_cleared"
                )
                open_row.version += 1
                return AlertEvaluation(
                    rule.rule_id, False, "resolved", self._occurrence_view(open_row)
                )
            return AlertEvaluation(rule.rule_id, False, "inactive", None)

    def acknowledge(
        self, occurrence_id: uuid.UUID, *, actor: str, now: datetime
    ) -> AlertOccurrence:
        if not actor or len(actor) > 255:
            raise ValueError("actor must contain 1..255 characters")
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            row = session.get(AlertOccurrenceRow, occurrence_id, with_for_update=True)
            if row is None:
                raise LookupError("alert occurrence does not exist")
            if row.status == AlertStatus.RESOLVED.value:
                raise ValueError("resolved alert cannot be acknowledged")
            if row.status == AlertStatus.ACKNOWLEDGED.value:
                if row.acknowledged_by != actor:
                    raise ValueError("alert is already acknowledged by another actor")
                return self._occurrence_view(row)
            row.status = AlertStatus.ACKNOWLEDGED.value
            row.acknowledged_at = event_time
            row.acknowledged_by = actor
            row.version += 1
            return self._occurrence_view(row)

    def resolve(
        self, occurrence_id: uuid.UUID, *, actor: str, reason: str, now: datetime
    ) -> AlertOccurrence:
        if not actor or not reason or len(actor) > 255 or len(reason) > 500:
            raise ValueError("actor and reason are required and bounded")
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            row = session.get(AlertOccurrenceRow, occurrence_id, with_for_update=True)
            if row is None:
                raise LookupError("alert occurrence does not exist")
            if row.status == AlertStatus.RESOLVED.value:
                return self._occurrence_view(row)
            row.status = AlertStatus.RESOLVED.value
            row.resolved_at = event_time
            row.resolve_reason = f"manual:{actor}:{reason}"[:500]
            row.version += 1
            return self._occurrence_view(row)

    @staticmethod
    def _rule_view(row: AlertRuleRow) -> AlertRule:
        return AlertRule(
            rule_id=row.rule_id,
            name=row.name,
            metric_name=row.metric_name,
            comparator=Comparator(row.comparator),
            threshold=row.threshold,
            window_seconds=row.window_seconds,
            min_samples=row.min_samples,
            aggregation=Aggregation(row.aggregation),
            enabled=row.enabled,
        )

    @staticmethod
    def _occurrence_view(row: AlertOccurrenceRow) -> AlertOccurrence:
        return AlertOccurrence(
            occurrence_id=row.occurrence_id,
            rule_id=row.rule_id,
            status=AlertStatus(row.status),
            observed_value=row.observed_value,
            sample_count=row.sample_count,
            window_start=ensure_utc(row.window_start),
            window_end=ensure_utc(row.window_end),
            fired_at=ensure_utc(row.fired_at),
            version=row.version,
            acknowledged_at=(ensure_utc(row.acknowledged_at) if row.acknowledged_at else None),
            acknowledged_by=row.acknowledged_by,
            resolved_at=ensure_utc(row.resolved_at) if row.resolved_at else None,
            resolve_reason=row.resolve_reason,
        )


class AlertService:
    def __init__(self, repository: SqlAlchemyAlertRepository, metrics: MetricReader) -> None:
        self.repository = repository
        self.metrics = metrics

    def evaluate(self, rule_id: uuid.UUID, *, now: datetime) -> AlertEvaluation:
        event_time = require_aware(now, field="now")
        rule = self.repository.get_rule(rule_id)
        start = event_time - timedelta(seconds=rule.window_seconds)
        observation = self.metrics.observe(
            rule.metric_name,
            aggregation=rule.aggregation,
            window_start=start,
            window_end=event_time,
        )
        if observation.metric_name != rule.metric_name:
            raise ValueError("metric reader returned the wrong metric")
        if (
            observation.window_start.astimezone(UTC) != start
            or observation.window_end.astimezone(UTC) != event_time
        ):
            raise ValueError("metric reader returned the wrong window")
        return self.repository.apply_observation(rule, observation, now=event_time)
