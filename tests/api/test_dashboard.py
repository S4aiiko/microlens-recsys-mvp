from __future__ import annotations

import csv
import io
import unittest
from datetime import timedelta

from apps.api.app.api.admin.csv_export import CSV_COLUMNS, dashboard_csv, formula_safe
from apps.api.app.api.admin.queries import DashboardQueryService
from apps.api.app.db.models import FeedType, Item, OnlineStatus
from apps.api.app.events import (
    EventRequest,
    EventService,
    PageExposure,
    SnapshotCandidate,
    SnapshotService,
)

from ._support import NOW, add_user, factory_for, sqlite_engine


class DashboardQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.queries = DashboardQueryService()
        self.snapshots = SnapshotService()
        self.events = EventService()
        with self.factory.begin() as session:
            self.user = add_user(session, username="dashboard-user")
            session.add_all(
                [
                    Item(id="=formula-item", title="=SUM(A1:A2)"),
                    Item(
                        id="offline-zero", title="No activity", online_status=OnlineStatus.OFFLINE
                    ),
                    Item(id="online-zero", title="No activity online"),
                ]
            )
        with self.factory.begin() as session:
            snapshot = self.snapshots.create_snapshot(
                session,
                user_id=self.user.id,
                feed_type=FeedType.POPULAR,
                model_version="model-dashboard",
                snapshot_seed=1,
                expires_at=NOW + timedelta(hours=3),
                candidates=[SnapshotCandidate("=formula-item", "popular", 1.0, 1.0, 0)],
                now=NOW,
            )
            self.snapshot_id = snapshot.snapshot_id
        with self.factory.begin() as session:
            page = self.snapshots.record_page(
                session,
                snapshot_id=self.snapshot_id,
                user_id=self.user.id,
                offset=0,
                limit=1,
                latency_ms=2,
                page=[PageExposure("=formula-item", 0, "popular")],
                now=NOW,
            )
            self.request_id = page.request_id
        for kind, duration in (("click", None), ("like", None), ("dwell", 2_000)):
            payload = EventRequest.model_validate(
                {
                    "event_id": __import__("uuid").uuid4(),
                    "request_id": self.request_id,
                    "item_id": "=formula-item",
                    "position": 0,
                    "event_type": kind,
                    "client_timestamp": NOW,
                    "duration_ms": duration,
                }
            )
            with self.factory.begin() as session:
                self.events.submit(session, user_id=self.user.id, request=payload, now=NOW)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_overview_timeseries_feeds_hot_and_debug_are_db_derived(self) -> None:
        start = NOW - timedelta(minutes=15)
        end = NOW + timedelta(minutes=45)
        with self.factory() as session:
            overview = self.queries.overview(session, from_utc=start, to_utc=end)
            self.assertEqual(overview.total_users, 1)
            self.assertEqual(overview.active_users, 1)
            self.assertEqual(overview.requests, 1)
            self.assertEqual(overview.exposures, 1)
            self.assertEqual((overview.clicks, overview.likes), (1, 1))
            self.assertEqual(overview.dwell_ms_total, 2_000)
            self.assertEqual(overview.offline_item_count, 1)
            self.assertEqual(overview.ctr, 1.0)
            self.assertFalse(overview.zero_denominator)

            buckets = self.queries.timeseries(
                session, from_utc=start, to_utc=end, feed_type=FeedType.POPULAR
            )
            self.assertEqual(len(buckets), 1)
            self.assertEqual(buckets[0].request_count, 1)
            self.assertEqual(buckets[0].active_user_count, 1)
            self.assertEqual(buckets[0].dwell_ms_avg, 2_000)
            feeds = self.queries.feeds(session, from_utc=start, to_utc=end)
            self.assertEqual(feeds.feeds[0].feed_type, "popular")
            self.assertEqual(
                feeds.feed_share,
                {"personalized": 0.0, "popular": 1.0, "explore": 0.0},
            )
            hot = self.queries.hot_items(session, from_utc=start, to_utc=end, limit=1)
            self.assertEqual(hot[0].item_id, "=formula-item")
            self.assertEqual(len(hot), 1)
            user_debug = self.queries.user_debug(session, self.user.id)
            self.assertEqual(user_debug.recent_request_ids, [self.request_id])
            request_debug = self.queries.request_debug(session, self.request_id)
            self.assertEqual(request_debug.candidate_item_ids, ["=formula-item"])
            self.assertEqual(len(request_debug.events), 4)

            empty = self.queries.overview(
                session,
                from_utc=NOW - timedelta(days=2),
                to_utc=NOW - timedelta(days=1),
            )
            self.assertEqual(empty.ctr, 0)
            self.assertTrue(empty.zero_denominator)
            empty_feeds = self.queries.feeds(
                session,
                from_utc=NOW - timedelta(days=2),
                to_utc=NOW - timedelta(days=1),
            )
            self.assertEqual(
                empty_feeds.feed_share,
                {"personalized": 0.0, "popular": 0.0, "explore": 0.0},
            )
            self.assertEqual(
                self.queries.hot_items(
                    session,
                    from_utc=NOW - timedelta(days=2),
                    to_utc=NOW - timedelta(days=1),
                ),
                [],
            )

    def test_csv_bom_rfc4180_columns_order_filters_and_formula_safety(self) -> None:
        with self.factory() as session:
            buckets = self.queries.timeseries(
                session,
                from_utc=NOW - timedelta(minutes=15),
                to_utc=NOW + timedelta(minutes=45),
                feed_type=FeedType.POPULAR,
            )
        payload = dashboard_csv(buckets)
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        text = payload[3:].decode("utf-8")
        self.assertIn("\r\n", text)
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual(rows[1][2], "popular")
        for value in ("=cmd", "+cmd", "-cmd", "@cmd"):
            self.assertEqual(formula_safe(value), "'" + value)


if __name__ == "__main__":
    unittest.main()
