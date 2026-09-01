from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from recsys.analytics.contracts import TimeWindow
from recsys.analytics.exporter import AnalyticsExporter
from recsys.analytics.postgres import PostgreSQLAnalyticsSource
from recsys.analytics.reconcile import reconcile_with_pyarrow


def _exercise_source(engine) -> None:
    from apps.api.app.db.models import FeedType, Item
    from apps.api.app.events import (
        EventRequest,
        EventService,
        PageExposure,
        SnapshotCandidate,
        SnapshotService,
    )
    from tests.api._support import NOW, add_user, factory_for

    factory = factory_for(engine)
    snapshots = SnapshotService()
    events = EventService()
    suffix = __import__("uuid").uuid4().hex
    item_id = f"analytics-item-{suffix}"
    with factory.begin() as session:
        user = add_user(session, username=f"analytics-{suffix}")
        session.add(Item(id=item_id, title="Analytics item"))
    with factory.begin() as session:
        snapshot = snapshots.create_snapshot(
            session,
            user_id=user.id,
            feed_type=FeedType.EXPLORE,
            model_version="analytics-model",
            snapshot_seed=7,
            expires_at=NOW + timedelta(hours=1),
            candidates=[SnapshotCandidate(item_id, "explore", 1.0, 1.0, 0)],
            now=NOW,
        )
        page = snapshots.record_page(
            session,
            snapshot_id=snapshot.snapshot_id,
            user_id=user.id,
            offset=0,
            limit=1,
            latency_ms=3,
            page=[PageExposure(item_id, 0, "explore")],
            now=NOW,
        )
    with factory.begin() as session:
        events.submit(
            session,
            user_id=user.id,
            request=EventRequest.model_validate(
                {
                    "event_id": __import__("uuid").uuid4(),
                    "request_id": page.request_id,
                    "item_id": item_id,
                    "position": 0,
                    "event_type": "click",
                    "client_timestamp": NOW,
                }
            ),
            now=NOW + timedelta(seconds=1),
        )
    window = TimeWindow(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    with factory() as session:
        captured = PostgreSQLAnalyticsSource(session).collect(
            window, previous_event_sequence_exclusive=0
        )
    own_events = [row for row in captured.events if row.user_id == str(user.id)]
    own_exposures = [row for row in captured.exposures if row.user_id == str(user.id)]
    if [row.event_type for row in own_events] != ["impression", "click"]:
        raise AssertionError("source did not return the expected ordered event pair")
    if len(own_exposures) != 1 or own_exposures[0].feed_type != "explore":
        raise AssertionError("source did not return the expected exposure")
    if captured.postgres_event_counts["impression"] < 1:
        raise AssertionError("PostgreSQL impression aggregate is missing")
    if captured.postgres_event_counts["click"] < 1:
        raise AssertionError("PostgreSQL click aggregate is missing")
    if captured.postgres_exposure_counts["explore"] < 1:
        raise AssertionError("PostgreSQL explore aggregate is missing")
    with tempfile.TemporaryDirectory() as temporary:
        published = AnalyticsExporter().publish(captured, Path(temporary))
        reconciled = reconcile_with_pyarrow(published.path)
        if not reconciled.matched or reconciled.postgres != reconciled.parquet:
            raise AssertionError("PyArrow Parquet counts do not reconcile with PostgreSQL")


class PostgreSQLAnalyticsSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from apps.api.app.db.models import FeedType, Item
            from apps.api.app.events import (
                EventRequest,
                EventService,
                PageExposure,
                SnapshotCandidate,
                SnapshotService,
            )
            from tests.api._support import NOW, add_user, factory_for, sqlite_engine
        except ModuleNotFoundError as exc:
            self.skipTest(f"exact API dependency environment unavailable: {exc.name}")
        self.now = NOW
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        snapshots = SnapshotService()
        events = EventService()
        with self.factory.begin() as session:
            user = add_user(session, username="analytics-source-user")
            session.add(Item(id="analytics-item", title="Analytics item"))
        with self.factory.begin() as session:
            snapshot = snapshots.create_snapshot(
                session,
                user_id=user.id,
                feed_type=FeedType.EXPLORE,
                model_version="analytics-model",
                snapshot_seed=7,
                expires_at=NOW + timedelta(hours=1),
                candidates=[SnapshotCandidate("analytics-item", "explore", 1.0, 1.0, 0)],
                now=NOW,
            )
            page = snapshots.record_page(
                session,
                snapshot_id=snapshot.snapshot_id,
                user_id=user.id,
                offset=0,
                limit=1,
                latency_ms=3,
                page=[PageExposure("analytics-item", 0, "explore")],
                now=NOW,
            )
        with self.factory.begin() as session:
            events.submit(
                session,
                user_id=user.id,
                request=EventRequest.model_validate(
                    {
                        "event_id": __import__("uuid").uuid4(),
                        "request_id": page.request_id,
                        "item_id": "analytics-item",
                        "position": 0,
                        "event_type": "click",
                        "client_timestamp": NOW,
                    }
                ),
                now=NOW + timedelta(seconds=1),
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_sql_rows_and_independent_group_counts_share_window_and_watermark(self) -> None:
        window = TimeWindow(self.now - timedelta(minutes=1), self.now + timedelta(minutes=1))
        with self.factory() as session:
            snapshot = PostgreSQLAnalyticsSource(session).collect(
                window, previous_event_sequence_exclusive=0
            )
        self.assertEqual(snapshot.event_sequence_cutoff_inclusive, 2)
        self.assertEqual([row.event_type for row in snapshot.events], ["impression", "click"])
        self.assertEqual(snapshot.postgres_event_counts["impression"], 1)
        self.assertEqual(snapshot.postgres_event_counts["click"], 1)
        self.assertEqual(snapshot.postgres_exposure_counts["explore"], 1)
        with self.factory() as session:
            empty = PostgreSQLAnalyticsSource(session).collect(
                window, previous_event_sequence_exclusive=2
            )
        self.assertEqual(empty.events, ())
        self.assertEqual(empty.exposures, ())
        self.assertEqual(empty.event_sequence_cutoff_inclusive, 2)


class LivePostgreSQLAnalyticsSourceTests(unittest.TestCase):
    def test_opt_in_real_postgresql_snapshot_and_aggregate_reconciliation(self) -> None:
        database_url = os.environ.get("ANALYTICS_TEST_DATABASE_URL")
        if not database_url:
            self.skipTest("set ANALYTICS_TEST_DATABASE_URL to a dedicated migrated PostgreSQL DB")
        from sqlalchemy import create_engine

        engine = create_engine(database_url)
        try:
            if engine.dialect.name != "postgresql":
                self.fail("ANALYTICS_TEST_DATABASE_URL must use PostgreSQL")
            _exercise_source(engine)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
