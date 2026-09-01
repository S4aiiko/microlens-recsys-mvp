from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from apps.api.app.alerts.domain import Aggregation, AlertRule, AlertStatus, Comparator
from apps.api.app.alerts.service import (
    AlertService,
    SqlAlchemyAlertRepository,
    WindowedMetricReader,
)
from tests.async_platform._support import NOW, runtime


class TimedSamples:
    def __init__(self) -> None:
        self.values: dict[str, list[tuple[object, float]]] = {}

    def add(self, metric: str, at, value: float) -> None:
        self.values.setdefault(metric, []).append((at, value))

    def samples(self, metric_name: str, *, window_start, window_end):
        return [
            value
            for at, value in self.values.get(metric_name, [])
            if window_start <= at < window_end
        ]


class AlertTests(unittest.TestCase):
    def setUp(self) -> None:
        factory, _, _ = runtime()
        self.repository = SqlAlchemyAlertRepository(factory)
        self.samples = TimedSamples()
        self.service = AlertService(self.repository, WindowedMetricReader(self.samples))
        self.rule = AlertRule(
            rule_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            name="high-click-errors",
            metric_name="click_error_rate",
            comparator=Comparator.GTE,
            threshold=0.25,
            window_seconds=60,
            min_samples=3,
            aggregation=Aggregation.AVG,
        )
        self.repository.add_rule(self.rule, now=NOW - timedelta(minutes=2))

    def test_real_window_samples_fire_acknowledge_and_resolve(self) -> None:
        for seconds, value in [(50, 0.2), (40, 0.4), (30, 0.3)]:
            self.samples.add("click_error_rate", NOW - timedelta(seconds=seconds), value)
        fired = self.service.evaluate(self.rule.rule_id, now=NOW)
        self.assertTrue(fired.condition_met)
        self.assertEqual(fired.transition, "fired")
        assert fired.occurrence is not None
        self.assertAlmostEqual(fired.occurrence.observed_value, 0.3)

        acknowledged = self.repository.acknowledge(
            fired.occurrence.occurrence_id, actor="operator-1", now=NOW + timedelta(seconds=1)
        )
        self.assertEqual(acknowledged.status, AlertStatus.ACKNOWLEDGED)
        still = self.service.evaluate(self.rule.rule_id, now=NOW + timedelta(seconds=2))
        assert still.occurrence is not None
        self.assertEqual(still.occurrence.status, AlertStatus.ACKNOWLEDGED)
        self.assertEqual(still.transition, "still_firing")

        cleared = self.service.evaluate(self.rule.rule_id, now=NOW + timedelta(minutes=2))
        assert cleared.occurrence is not None
        self.assertEqual(cleared.transition, "resolved")
        self.assertEqual(cleared.occurrence.status, AlertStatus.RESOLVED)
        self.assertEqual(cleared.occurrence.resolve_reason, "insufficient_samples")

    def test_manual_resolution_and_new_breach_create_distinct_occurrences(self) -> None:
        for seconds in (30, 20, 10):
            self.samples.add("click_error_rate", NOW - timedelta(seconds=seconds), 0.5)
        first = self.service.evaluate(self.rule.rule_id, now=NOW)
        assert first.occurrence is not None
        resolved = self.repository.resolve(
            first.occurrence.occurrence_id,
            actor="admin-1",
            reason="incident mitigated",
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(resolved.status, AlertStatus.RESOLVED)
        second = self.service.evaluate(self.rule.rule_id, now=NOW + timedelta(seconds=2))
        assert second.occurrence is not None
        self.assertNotEqual(first.occurrence.occurrence_id, second.occurrence.occurrence_id)
        self.assertEqual(second.transition, "fired")

    def test_below_threshold_never_creates_alert(self) -> None:
        for seconds in (30, 20, 10):
            self.samples.add("click_error_rate", NOW - timedelta(seconds=seconds), 0.1)
        evaluation = self.service.evaluate(self.rule.rule_id, now=NOW)
        self.assertEqual(evaluation.transition, "inactive")
        self.assertIsNone(evaluation.occurrence)

    def test_non_finite_thresholds_and_samples_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            AlertRule(
                rule_id=uuid.uuid4(),
                name="invalid",
                metric_name="click_error_rate",
                comparator=Comparator.GTE,
                threshold=float("nan"),
                window_seconds=60,
            )
        self.samples.add("click_error_rate", NOW - timedelta(seconds=1), float("inf"))
        with self.assertRaises(ValueError):
            self.service.evaluate(self.rule.rule_id, now=NOW)


if __name__ == "__main__":
    unittest.main()
