from __future__ import annotations

import os
import unittest
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.cache import InMemoryCacheBackend, VersionedCache
from apps.api.app.db.models import (
    AccountStatus,
    Event,
    EventType,
    Exposure,
    FeedType,
    Item,
    OnlineStatus,
    RecommendationRequest,
    RecommendationSnapshot,
    RecommendationSnapshotItem,
    Role,
    User,
    UserProfile,
)
from apps.api.app.events.service import SnapshotService
from apps.api.app.feeds.cursor import CursorCodec
from apps.api.app.feeds.service import RecommendationConfig, RecommendationService

DATABASE_URL = os.environ.get("PHASE4_TEST_DATABASE_URL")


class FailingPageSnapshotService(SnapshotService):
    def record_page(self, *args, **kwargs):
        raise RuntimeError("injected PostgreSQL page failure")


def _service(now: datetime, *, snapshots: SnapshotService | None = None) -> RecommendationService:
    return RecommendationService(
        model_provider=lambda: (None, None),
        cache=VersionedCache(InMemoryCacheBackend()),
        cursor_codec=CursorCodec("phase-4-postgres-cursor-secret-is-long-enough"),
        snapshot_service=snapshots,
        config=RecommendationConfig(
            candidate_pool_size=20,
            topic_dedup_enabled=False,
            mmr_enabled=False,
        ),
        clock=lambda: now,
    )


def _run_postgres_feed_persistence_offline_fill_and_atomic_rollback() -> None:
    assert DATABASE_URL is not None
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        expire_on_commit=False,
        autoflush=False,
    )
    suffix = uuid.uuid4().hex
    user_id = uuid.uuid4()
    item_ids = [f"phase4-pg-{suffix}-{index}" for index in range(6)]
    now = datetime.now(UTC)
    try:
        with factory.begin() as session:
            session.add(
                User(
                    id=user_id,
                    username=f"phase4_pg_{suffix}",
                    username_normalized=f"phase4_pg_{suffix}",
                    password_hash="phase4-test-only-not-for-login",
                    role=Role.USER,
                    status=AccountStatus.ENABLED,
                )
            )
            session.add(UserProfile(user_id=user_id))
            session.add_all(
                [
                    Item(
                        id=item_id,
                        title=f"Phase 4 PostgreSQL item {index}",
                        likes_snapshot=10**15 - index,
                        views_snapshot=10**15 - index,
                    )
                    for index, item_id in enumerate(item_ids)
                ]
            )

        service = _service(now)
        with factory.begin() as session:
            first = service.get_page(session, user_id=user_id, feed_type=FeedType.POPULAR, limit=2)
        with factory.begin() as session:
            next_snapshot_item = session.scalar(
                select(RecommendationSnapshotItem).where(
                    RecommendationSnapshotItem.snapshot_id == first.page.snapshot_id,
                    RecommendationSnapshotItem.snapshot_position == 2,
                )
            )
            assert next_snapshot_item is not None
            offline_item_id = next_snapshot_item.item_id
            session.get(Item, offline_item_id).online_status = OnlineStatus.OFFLINE

        assert first.page.next_cursor
        with factory.begin() as session:
            second = service.get_page(
                session,
                user_id=user_id,
                feed_type=FeedType.POPULAR,
                limit=2,
                cursor=first.page.next_cursor,
            )
        assert second.page.request_id != first.page.request_id
        assert [item.position for item in second.page.items] == [2, 3]
        assert offline_item_id not in {item.item_id for item in second.page.items}
        assert not (
            {item.item_id for item in first.page.items}
            & {item.item_id for item in second.page.items}
        )

        with factory() as session:
            assert (
                session.scalar(
                    select(func.count(RecommendationRequest.request_id)).where(
                        RecommendationRequest.snapshot_id == first.page.snapshot_id
                    )
                )
                == 2
            )
            assert (
                session.scalar(select(func.count(Exposure.id)).where(Exposure.user_id == user_id))
                == 4
            )
            assert (
                session.scalar(
                    select(func.count(Event.id)).where(
                        Event.user_id == user_id,
                        Event.event_type == EventType.IMPRESSION,
                    )
                )
                == 4
            )
            baseline_snapshots = session.scalar(
                select(func.count(RecommendationSnapshot.snapshot_id)).where(
                    RecommendationSnapshot.user_id == user_id
                )
            )

        failing = _service(now, snapshots=FailingPageSnapshotService())
        with factory() as session:
            try:
                failing.get_page(session, user_id=user_id, feed_type=FeedType.EXPLORE, limit=2)
            except RuntimeError as exc:
                assert "injected PostgreSQL" in str(exc)
            else:
                raise AssertionError("injected PostgreSQL page failure was not raised")
            session.rollback()
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count(RecommendationSnapshot.snapshot_id)).where(
                        RecommendationSnapshot.user_id == user_id
                    )
                )
                == baseline_snapshots
            )
    finally:
        with factory.begin() as session:
            session.execute(delete(User).where(User.id == user_id))
            session.execute(delete(Item).where(Item.id.in_(item_ids)))
        engine.dispose()


@unittest.skipUnless(
    DATABASE_URL,
    "set PHASE4_TEST_DATABASE_URL to an isolated migrated PostgreSQL database",
)
class PostgreSQLRecommendationIntegrationTests(unittest.TestCase):
    def test_feed_persistence_offline_fill_and_atomic_rollback(self) -> None:
        _run_postgres_feed_persistence_offline_fill_and_atomic_rollback()
