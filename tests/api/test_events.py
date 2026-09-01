from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from apps.api.app.auth import (
    AuthService,
    CookieSettings,
    JWTService,
    JWTSettings,
    PasswordService,
    build_auth_dependencies,
    build_auth_router,
    install_api_error_handlers,
)
from apps.api.app.auth.rate_limit import InMemoryRegistrationLimiter
from apps.api.app.db.models import (
    Event,
    EventType,
    Exposure,
    FeedType,
    Item,
    OnlineStatus,
    UserProfile,
)
from apps.api.app.db.session import session_dependency
from apps.api.app.events import (
    EventBatchRequest,
    EventRequest,
    EventService,
    PageExposure,
    SnapshotCandidate,
    SnapshotService,
    TrainingExportRepository,
    build_events_router,
)

from ._support import NOW, PASSWORD, add_user, factory_for, sqlite_engine


class EventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.snapshots = SnapshotService()
        self.events = EventService()
        with self.factory.begin() as session:
            self.user = add_user(session, username="event-user")
            self.other = add_user(session, username="other-user")
            session.add_all(
                [
                    Item(id="item-1", title="One"),
                    Item(id="item-2", title="Two"),
                    Item(id="item-3", title="Three"),
                ]
            )
        with self.factory.begin() as session:
            snapshot = self.snapshots.create_snapshot(
                session,
                user_id=self.user.id,
                feed_type=FeedType.PERSONALIZED,
                model_version="model-v1",
                snapshot_seed=17,
                expires_at=NOW + timedelta(hours=1),
                candidates=[
                    SnapshotCandidate("item-1", "dssm", 1.0, 1.0, 0),
                    SnapshotCandidate("item-2", "popular", 0.5, 0.5, 1),
                    SnapshotCandidate("item-3", "explore", 0.25, 0.25, 2),
                ],
                now=NOW,
            )
            self.snapshot_id = snapshot.snapshot_id
        with self.factory.begin() as session:
            page = self.snapshots.record_page(
                session,
                snapshot_id=self.snapshot_id,
                user_id=self.user.id,
                offset=0,
                limit=2,
                latency_ms=5,
                page=[
                    PageExposure("item-1", 0, "dssm"),
                    PageExposure("item-2", 1, "popular"),
                ],
                now=NOW,
            )
            self.request_id = page.request_id

    def tearDown(self) -> None:
        self.engine.dispose()

    def _event(
        self,
        kind: str,
        *,
        item: str = "item-1",
        position: int = 0,
        duration: int | None = None,
        event_id: uuid.UUID | None = None,
        request_id: uuid.UUID | None = None,
    ) -> EventRequest:
        data = {
            "event_id": event_id or uuid.uuid4(),
            "request_id": request_id or self.request_id,
            "item_id": item,
            "position": position,
            "event_type": kind,
            "client_timestamp": NOW,
            "payload": {"fixture": True},
        }
        if duration is not None:
            data["duration_ms"] = duration
        return EventRequest.model_validate(data)

    def test_canonical_impression_exactly_once_and_six_client_behaviors_idempotent(self) -> None:
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count(Exposure.id))), 2)
            impressions = session.scalar(
                select(func.count(Event.id)).where(Event.event_type == EventType.IMPRESSION)
            )
            self.assertEqual(impressions, 2)
            distinct_exposures = session.scalar(
                select(func.count(func.distinct(Event.exposure_id))).where(
                    Event.event_type == EventType.IMPRESSION
                )
            )
            self.assertEqual(distinct_exposures, 2)

        requests = [
            self._event("click"),
            self._event("like"),
            self._event("not_interested"),
            self._event("dwell", duration=1_500),
            self._event("revisit"),
            self._event("share"),
        ]
        for request in requests:
            with self.factory.begin() as session:
                result = self.events.submit(session, user_id=self.user.id, request=request, now=NOW)
                self.assertEqual(result.status, "accepted")
            with self.factory.begin() as session:
                replay = self.events.submit(session, user_id=self.user.id, request=request, now=NOW)
                self.assertEqual(replay.status, "duplicate")
        with self.factory() as session:
            profile = session.get(UserProfile, self.user.id)
            assert profile is not None
            self.assertEqual(profile.profile_version, 6)
            self.assertEqual(profile.dwell_summary["duration_ms_total"], 1_500)
            self.assertEqual(
                profile.title_preferences["one"],
                {"positive": 10, "negative": 4, "score": 6},
            )
            self.assertLessEqual(len(profile.title_preferences), 64)
            self.assertEqual(session.scalar(select(func.count(Event.id))), 8)

    def test_page_rejects_duplicate_items_over_limit_and_position_drift(self) -> None:
        invalid_pages = [
            (0, 2, [PageExposure("item-1", 0, "dssm")] * 2),
            (
                0,
                1,
                [PageExposure("item-1", 0, "dssm"), PageExposure("item-2", 1, "popular")],
            ),
            (1, 1, [PageExposure("item-2", 0, "popular")]),
            (
                0,
                2,
                [PageExposure("item-3", 0, "explore"), PageExposure("item-1", 1, "dssm")],
            ),
        ]
        for offset, limit, page in invalid_pages:
            with self.subTest(offset=offset, limit=limit, page=page):
                with self.factory.begin() as session, self.assertRaises(ValueError):
                    self.snapshots.record_page(
                        session,
                        snapshot_id=self.snapshot_id,
                        user_id=self.user.id,
                        offset=offset,
                        limit=limit,
                        latency_ms=1,
                        page=page,
                        now=NOW,
                    )

    def test_offline_middle_candidate_is_backfilled_in_snapshot_order(self) -> None:
        with self.factory.begin() as session:
            session.get(Item, "item-2").online_status = OnlineStatus.OFFLINE
        with self.factory.begin() as session:
            page = self.snapshots.record_page(
                session,
                snapshot_id=self.snapshot_id,
                user_id=self.user.id,
                offset=0,
                limit=2,
                latency_ms=1,
                page=[
                    PageExposure("item-1", 0, "dssm"),
                    PageExposure("item-3", 1, "explore"),
                ],
                now=NOW,
            )
            request_id = page.request_id
        with self.factory() as session:
            exposures = list(
                session.scalars(
                    select(Exposure)
                    .where(Exposure.request_id == request_id)
                    .order_by(Exposure.position)
                )
            )
            self.assertEqual(
                [(row.item_id, row.position) for row in exposures],
                [("item-1", 0), ("item-3", 1)],
            )
            self.assertEqual(
                session.scalar(
                    select(func.count(Event.id)).where(
                        Event.request_id == request_id,
                        Event.event_type == EventType.IMPRESSION,
                    )
                ),
                2,
            )
        event = self._event(
            "click",
            item="item-3",
            position=1,
            request_id=request_id,
        )
        with self.factory.begin() as session:
            result = self.events.submit(session, user_id=self.user.id, request=event, now=NOW)
            self.assertEqual(result.status, "accepted")

    def test_forged_exposure_duration_and_client_impression_are_rejected(self) -> None:
        forged = [
            self._event("click", item="item-2", position=0),
            self._event("click", request_id=uuid.uuid4()),
        ]
        for request in forged:
            with self.factory.begin() as session:
                result = self.events.submit(session, user_id=self.user.id, request=request, now=NOW)
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.error_code, "exposure_mismatch")
        with self.factory.begin() as session:
            cross_user = self.events.submit(
                session, user_id=self.other.id, request=self._event("click"), now=NOW
            )
            self.assertEqual(cross_user.status, "rejected")
        with self.assertRaises(ValidationError):
            self._event("dwell")
        with self.assertRaises(ValidationError):
            EventRequest.model_validate(
                {
                    "event_id": uuid.uuid4(),
                    "request_id": self.request_id,
                    "item_id": "item-1",
                    "position": 0,
                    "event_type": "impression",
                    "client_timestamp": NOW,
                }
            )

    def test_batch_savepoints_partial_success_replay_and_fingerprint_conflict(self) -> None:
        accepted = self._event("click")
        forged = self._event("like", item="item-2", position=0)
        batch = EventBatchRequest(batch_id=uuid.uuid4(), events=[accepted, forged, accepted])
        with self.factory.begin() as session:
            response = self.events.submit_batch(
                session, user_id=self.user.id, request=batch, now=NOW
            )
        self.assertEqual((response.accepted, response.duplicate, response.rejected), (1, 1, 1))
        self.assertEqual(
            [row.status for row in response.results], ["accepted", "rejected", "duplicate"]
        )
        with self.factory.begin() as session:
            replay = self.events.submit_batch(session, user_id=self.user.id, request=batch, now=NOW)
            self.assertEqual(replay, response)

        conflict = self._event("share", event_id=accepted.event_id)
        with self.factory.begin() as session:
            result = self.events.submit(session, user_id=self.user.id, request=conflict, now=NOW)
            self.assertEqual(result.error_code, "event_id_conflict")
        with self.assertRaises(ValidationError):
            EventBatchRequest(batch_id=uuid.uuid4(), events=[accepted] * 101)

    def test_training_export_watermark_claim_order_and_cas(self) -> None:
        repository = TrainingExportRepository()
        with self.factory.begin() as session:
            claimed = repository.claim(session, name="online-events")
            rows = repository.events(session, claimed)
            self.assertEqual([row.id for row in rows], sorted(row.id for row in rows))
            self.assertTrue(repository.complete(session, claimed, checksum="a" * 64))
        with self.factory.begin() as session:
            next_range = repository.claim(session, name="online-events")
            self.assertEqual(next_range.start_exclusive, claimed.end_inclusive)

    def test_single_event_payload_conflict_is_http_409(self) -> None:
        auth = AuthService(
            PasswordService(),
            JWTService(JWTSettings(secret="events-api-test-secret-longer-than-32-bytes")),
        )
        get_session = session_dependency(self.factory)
        dependencies = build_auth_dependencies(get_session, auth)
        app = FastAPI()
        install_api_error_handlers(app)
        app.include_router(
            build_auth_router(
                get_session=get_session,
                service=auth,
                dependencies=dependencies,
                limiter=InMemoryRegistrationLimiter(limit=10),
                cookies=CookieSettings(secure=False),
            )
        )
        app.include_router(
            build_events_router(
                get_session=get_session,
                dependencies=dependencies,
                service=self.events,
            )
        )
        event = self._event("click")
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login", json={"username": "event-user", "password": PASSWORD}
            )
            self.assertEqual(login.status_code, 200)
            csrf = client.cookies.get("microlens_csrf")
            accepted = client.post(
                "/api/events",
                json=event.model_dump(mode="json"),
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(accepted.status_code, 200)
            conflict_payload = event.model_dump(mode="json")
            conflict_payload["payload"] = {"different": True}
            conflict = client.post(
                "/api/events",
                json=conflict_payload,
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()["code"], "event_id_conflict")


if __name__ == "__main__":
    unittest.main()
