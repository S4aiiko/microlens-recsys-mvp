from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import torch
from sqlalchemy import func, select

from apps.api.app.cache import CachePolicy, InMemoryCacheBackend, VersionedCache
from apps.api.app.cache.runtime import CacheBackend
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    Event,
    EventType,
    Exposure,
    FeedType,
    Item,
    ModelStatus,
    ModelVersion,
    OnlineStatus,
    OperationBatch,
    OperationBatchStatus,
    OperationType,
    PromotionRule,
    PromotionStatus,
    RecommendationRequest,
    RecommendationSnapshot,
    RecommendationSnapshotItem,
    Role,
    ScopeType,
    UserProfile,
)
from apps.api.app.events.schemas import EventRequest
from apps.api.app.events.service import EventService, SnapshotService
from apps.api.app.feeds.cursor import CursorCodec
from apps.api.app.feeds.domain import RecallCandidate
from apps.api.app.feeds.ranking import merge_recall
from apps.api.app.feeds.retrieval import CatalogItem, ItemItemIndex, rank_candidates
from apps.api.app.feeds.service import LOGGER, RecommendationConfig, RecommendationService
from recsys.models.text import TitleHashEncoder
from tests.api._support import add_user, factory_for, sqlite_engine

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
SECRET = "phase-4-service-test-cursor-secret-long-enough"


class FakeDSSM:
    def __init__(self, scores: list[list[float]], *, fail: bool = False) -> None:
        self.scores = torch.tensor(scores, dtype=torch.float32)
        self.fail = fail

    def eval(self) -> FakeDSSM:
        return self

    def all_item_embeddings(self) -> torch.Tensor:
        return torch.eye(self.scores.shape[1], dtype=torch.float32)

    def score_catalog(self, user_indices: torch.Tensor, _items: torch.Tensor) -> torch.Tensor:
        if self.fail:
            raise RuntimeError("model recall unavailable")
        return self.scores[user_indices]


class FakeDeepFM:
    def __init__(self) -> None:
        self.last_source_indices: torch.Tensor | None = None
        self.last_dense_features: torch.Tensor | None = None

    def eval(self) -> FakeDeepFM:
        return self

    def __call__(
        self,
        user_indices: torch.Tensor,
        item_indices: torch.Tensor,
        _source_indices: torch.Tensor,
        dense_features: torch.Tensor,
    ) -> torch.Tensor:
        self.last_source_indices = _source_indices.clone()
        self.last_dense_features = dense_features.clone()
        direction = torch.where(user_indices == 0, -1.0, 1.0)
        return direction * item_indices.float() + dense_features[:, 0] * 0.001


class FakeBundle:
    def __init__(self, item_ids: list[str], *, fail_recall: bool = False) -> None:
        self.model_version = "model-v1"
        self.data_version = "data-v1"
        self.manifest = {"data_manifest_checksum": "a" * 64}
        self.user_ids = ("source-a", "source-b")
        self.item_ids = tuple(item_ids)
        self.user_to_index = {value: index for index, value in enumerate(self.user_ids)}
        self.item_to_index = {value: index for index, value in enumerate(self.item_ids)}
        self.popularity = {
            item_id: float(len(item_ids) - index) for index, item_id in enumerate(item_ids)
        }
        self.title_encoder = TitleHashEncoder.fit(
            {item_id: f"topic {index} title" for index, item_id in enumerate(item_ids)},
            bucket_count=32,
        )
        self.dssm = FakeDSSM(
            [
                [float(len(item_ids) - index) for index in range(len(item_ids))],
                [float(index) for index in range(len(item_ids))],
            ],
            fail=fail_recall,
        )
        self.deepfm = FakeDeepFM()


class FailingBackend(CacheBackend):
    def get(self, key: str) -> bytes | None:
        raise ConnectionError(key)

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        raise ConnectionError(key)

    def delete(self, key: str) -> None:
        raise ConnectionError(key)

    def ping(self) -> bool:
        raise ConnectionError("redis disconnected")


class PoisonOnceBackend(CacheBackend):
    def __init__(self) -> None:
        self.poisoned = True
        self.values: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        if self.poisoned:
            return (
                b'{"candidates":[{"item_id":[],"reason":"x","raw_score":true,'
                b'"source":"x"}],"fallback_reasons":[]}'
            )
        return self.values.get(key)

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        del ttl_seconds
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.poisoned = False
        self.values.pop(key, None)

    def ping(self) -> bool:
        return True


class FailingPageSnapshotService(SnapshotService):
    def record_page(self, *args, **kwargs):
        raise RuntimeError("injected page persistence failure")


def add_items(session, count: int = 8) -> list[str]:
    item_ids = [f"item-{index}" for index in range(count)]
    session.add_all(
        [
            Item(
                id=item_id,
                title=f"distinct topic {index}",
                likes_snapshot=count - index,
                views_snapshot=(count - index) * 10,
                cover_ref=None,
            )
            for index, item_id in enumerate(item_ids)
        ]
    )
    session.flush()
    return item_ids


def activate_model(session, model_version: str = "model-v1") -> None:
    session.add(
        ModelVersion(
            model_version=model_version,
            data_version="data-v1",
            data_manifest_checksum="a" * 64,
            config_checksum="b" * 64,
            metrics={"ndcg@10": 0.1},
            artifact_uri=f"{model_version}/bundle.json",
            artifact_checksum="c" * 64,
            manifest_checksum="d" * 64,
            purpose=EvaluationPurpose.BASE_OFFICIAL,
            evaluation_comparability=Comparability.COMPARABLE,
            activation_eligible=True,
            status=ModelStatus.ACTIVE,
            trained_at=NOW,
            published_at=NOW,
        )
    )
    session.flush()


def build_service(
    *,
    bundle: object | None = None,
    backend: CacheBackend | None = None,
    snapshot_service: SnapshotService | None = None,
    diversity: bool = False,
    source_histories: dict[str, tuple[str, ...]] | None = None,
) -> RecommendationService:
    histories = source_histories or {}
    resource = (
        SimpleNamespace(
            model_version=bundle.model_version,
            data_version=bundle.data_version,
            data_manifest_checksum=bundle.manifest["data_manifest_checksum"],
            verified_status="checksum_verified",
            bundle=bundle,
            item_item_index=ItemItemIndex.from_histories(histories),
            source_histories=histories,
        )
        if bundle is not None
        else None
    )
    return RecommendationService(
        model_provider=lambda: (
            (getattr(resource, "model_version", None), resource)
            if resource is not None
            else (None, None)
        ),
        cache=VersionedCache(
            backend or InMemoryCacheBackend(),
            policy=CachePolicy(ttl_seconds=60, process_fallback_ttl_seconds=5),
        ),
        cursor_codec=CursorCodec(SECRET),
        snapshot_service=snapshot_service,
        config=RecommendationConfig(
            candidate_pool_size=20,
            default_page_size=2,
            topic_dedup_enabled=diversity,
            mmr_enabled=diversity,
        ),
        clock=lambda: NOW,
    )


@pytest.mark.parametrize("limit", [True, 0, -1, 101, 1.5, "2"])
def test_service_rejects_invalid_direct_limits(limit: object) -> None:
    service = build_service()
    with pytest.raises(ValueError, match="limit"):
        service.get_page(
            object(),  # validation happens before any database access
            user_id=uuid.uuid4(),
            feed_type=FeedType.POPULAR,
            limit=limit,  # type: ignore[arg-type]
        )


def test_recommendation_trace_uses_enabled_uvicorn_logger_without_secrets() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    production_logger = logging.getLogger("uvicorn.error")
    original_level = production_logger.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    production_logger.addHandler(handler)
    production_logger.setLevel(logging.INFO)
    result = None
    try:
        with factory() as session:
            add_items(session, 3)
            user = add_user(session, username="trace-logger-user")
            session.commit()
        assert LOGGER.isEnabledFor(logging.INFO)
        service = build_service()
        with factory() as session:
            result = service.get_page(session, user_id=user.id, feed_type=FeedType.POPULAR, limit=1)
            session.commit()
    finally:
        production_logger.removeHandler(handler)
        production_logger.setLevel(original_level)
        handler.close()
        engine.dispose()

    assert result is not None
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    trace = next(record for record in records if record.get("event") == "recommendation_page")
    assert trace["request_id"] == str(result.page.request_id)
    assert trace["snapshot_id"] == str(result.page.snapshot_id)
    encoded = json.dumps(trace, sort_keys=True).casefold()
    assert all(
        sensitive not in encoded
        for sensitive in ("authorization", "cookie", "csrf", "jwt", "secret", "token")
    )


def test_official_train_history_seeds_item_item_cf_without_online_profile() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            item_ids = add_items(session, 4)
            user = add_user(session, username="official-history-user")
            user.source_user_id = "source-a"
            activate_model(session)
            session.commit()
        service = build_service(
            bundle=FakeBundle(item_ids),
            source_histories={
                "source-a": ("item-0", "item-1"),
                "source-b": ("item-0", "item-2"),
            },
        )
        with factory() as session:
            result = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=4
            )
            session.commit()
        assert result.trace.source_counts["item_item_cf"] >= 1
        delivered = {item.item_id for item in result.page.items}
        assert delivered.isdisjoint({"item-0", "item-1"})
        assert "item-2" in delivered
        assert result.trace.filter_counts["official_train_history"] >= 2
    finally:
        engine.dispose()


def test_mismatched_resource_traces_fallback_and_never_uses_old_cf() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            item_ids = add_items(session, 4)
            user = add_user(session, username="mismatched-resource-user")
            user.source_user_id = "source-a"
            activate_model(session)
            session.commit()
        service = build_service(
            bundle=FakeBundle(item_ids),
            source_histories={
                "source-a": ("item-0", "item-1"),
                "source-b": ("item-0", "item-2"),
            },
        )
        _slot_version, resource = service.model_provider()
        mismatched = SimpleNamespace(**{**vars(resource), "data_version": "data-v2"})
        service.model_provider = lambda: ("model-v1", mismatched)
        with factory() as session:
            result = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=4
            )
            session.commit()
        assert (
            "runtime_serving_resource_not_aligned_with_database_active"
            in result.trace.fallback_reasons
        )
        assert "item_item_cf" not in result.trace.source_counts
    finally:
        engine.dispose()


def test_two_users_differ_with_active_dssm_deepfm_and_cold_user_falls_back() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            item_ids = add_items(session)
            user_a = add_user(session, username="model-user-a")
            user_b = add_user(session, username="model-user-b")
            cold = add_user(session, username="cold-user")
            user_a.source_user_id = "source-a"
            user_b.source_user_id = "source-b"
            activate_model(session)
            session.commit()
        service = build_service(bundle=FakeBundle(item_ids))
        with factory() as session:
            page_a = service.get_page(
                session, user_id=user_a.id, feed_type=FeedType.PERSONALIZED, limit=3
            )
            session.commit()
        with factory() as session:
            page_b = service.get_page(
                session, user_id=user_b.id, feed_type=FeedType.PERSONALIZED, limit=3
            )
            session.commit()
        with factory() as session:
            cold_page = service.get_page(
                session, user_id=cold.id, feed_type=FeedType.PERSONALIZED, limit=3
            )
            session.commit()
        assert [row.item_id for row in page_a.page.items] != [
            row.item_id for row in page_b.page.items
        ]
        assert cold_page.page.items
        assert "cold_user_or_model_unavailable" in cold_page.trace.fallback_reasons
    finally:
        engine.dispose()


def test_deepfm_uses_frozen_source_zero_and_six_feature_semantics() -> None:
    bundle = FakeBundle(["item-0", "item-1"])
    merged = merge_recall(
        [
            RecallCandidate("item-0", "popular", 100.0, "popular"),
            RecallCandidate("item-1", "popular", 0.0, "popular"),
            RecallCandidate("item-0", "dssm", 0.25, "dssm"),
            RecallCandidate("item-1", "dssm", 0.0, "dssm"),
        ]
    )
    catalog = {
        item_id: CatalogItem(item_id, f"topic {index} title", None, 0, 0)
        for index, item_id in enumerate(bundle.item_ids)
    }
    result = rank_candidates(
        merged=merged,
        catalog=catalog,
        bundle=bundle,
        source_user_id="source-a",
        positive_history_titles=["topic 0 title"],
        profile_activity_count=3,
    )
    assert result.candidates
    assert bundle.deepfm.last_source_indices is not None
    assert bundle.deepfm.last_source_indices.tolist() == [0, 0]
    dense = bundle.deepfm.last_dense_features
    assert dense is not None and dense.shape == (2, 6)
    item_zero_row = bundle.item_to_index["item-0"]
    assert dense[item_zero_row, 0].item() == pytest.approx(0.25)
    assert 0.0 <= dense[item_zero_row, 1].item() <= 1.0
    assert dense[item_zero_row, 3].item() == pytest.approx(1.0 - dense[item_zero_row, 2].item())
    assert 0.0 <= dense[item_zero_row, 4].item() <= 1.0
    assert dense[item_zero_row, 5].item() == 1.0


def test_cursor_pages_keep_snapshot_unique_requests_and_fill_after_offline() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            add_items(session)
            user = add_user(session, username="cursor-user")
            session.commit()
        service = build_service()
        with factory() as session:
            first = service.get_page(session, user_id=user.id, feed_type=FeedType.POPULAR, limit=2)
            session.commit()
        assert first.page.next_cursor
        with factory() as session:
            third = session.scalar(
                select(RecommendationSnapshotItem).where(
                    RecommendationSnapshotItem.snapshot_id == first.page.snapshot_id,
                    RecommendationSnapshotItem.snapshot_position == 2,
                )
            )
            assert third is not None
            session.get(Item, third.item_id).online_status = OnlineStatus.OFFLINE
            session.commit()
        with factory() as session:
            second = service.get_page(
                session,
                user_id=user.id,
                feed_type=FeedType.POPULAR,
                limit=2,
                cursor=first.page.next_cursor,
            )
            session.commit()
        assert second.page.snapshot_id == first.page.snapshot_id
        assert second.page.model_version == first.page.model_version
        assert second.page.request_id != first.page.request_id
        assert [row.position for row in second.page.items] == [2, 3]
        assert not (
            {row.item_id for row in first.page.items} & {row.item_id for row in second.page.items}
        )
        assert third.item_id not in {row.item_id for row in second.page.items}
        with factory() as session:
            assert session.scalar(select(func.count(RecommendationRequest.request_id))) == 2
            assert session.scalar(select(func.count(Exposure.id))) == 4
            assert (
                session.scalar(
                    select(func.count(Event.id)).where(Event.event_type == EventType.IMPRESSION)
                )
                == 4
            )
    finally:
        engine.dispose()


def test_cache_hit_still_rechecks_viewed_items_and_profile_change_invalidates() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            add_items(session)
            user = add_user(session, username="cache-user")
            session.commit()
        service = build_service()
        with factory() as session:
            first = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=2
            )
            session.commit()
        with factory() as session:
            second = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=2
            )
            session.commit()
        assert first.trace.cache_status == "miss"
        assert second.trace.cache_status == "hit"
        assert not (
            {row.item_id for row in first.page.items} & {row.item_id for row in second.page.items}
        )
        with factory() as session:
            profile = session.get(UserProfile, user.id)
            profile.profile_version += 1
            session.commit()
        with factory() as session:
            changed = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=2
            )
            session.commit()
        assert changed.trace.cache_status == "miss"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "event_type", ["click", "like", "not_interested", "dwell", "revisit", "share"]
)
def test_each_client_behavior_changes_next_snapshot_but_old_cursor_is_stable(
    event_type: str,
) -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            add_items(session)
            user = add_user(session, username=f"behavior-{event_type}")
            session.commit()
        service = build_service()
        with factory() as session:
            before = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=1
            )
            session.commit()
        exposed = before.page.items[0]
        with factory() as session:
            result = EventService().submit(
                session,
                user_id=user.id,
                request=EventRequest(
                    event_id=uuid.uuid4(),
                    request_id=before.page.request_id,
                    item_id=exposed.item_id,
                    position=exposed.position,
                    event_type=event_type,
                    client_timestamp=NOW,
                    duration_ms=1_500 if event_type == "dwell" else None,
                ),
                now=NOW + timedelta(seconds=1),
            )
            assert result.status == "accepted"
            session.commit()
        with factory() as session:
            after = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=1
            )
            session.commit()
        assert after.page.snapshot_id != before.page.snapshot_id
        assert after.page.items[0].item_id != exposed.item_id
        assert after.trace.cache_status == "miss"
        with factory() as session:
            profile = session.get(UserProfile, user.id)
            assert profile.profile_version == 1
        if before.page.next_cursor:
            with factory() as session:
                old_page = service.get_page(
                    session,
                    user_id=user.id,
                    feed_type=FeedType.PERSONALIZED,
                    limit=1,
                    cursor=before.page.next_cursor,
                )
                session.commit()
            assert old_page.page.snapshot_id == before.page.snapshot_id
    finally:
        engine.dispose()


def test_redis_disconnect_model_failure_and_corrupt_cache_fail_closed() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            item_ids = add_items(session)
            user = add_user(session, username="fallback-user")
            user.source_user_id = "source-a"
            activate_model(session)
            session.commit()
        service = build_service(
            bundle=FakeBundle(item_ids, fail_recall=True), backend=FailingBackend()
        )
        with factory() as session:
            first = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=1
            )
            session.commit()
        with factory() as session:
            second = service.get_page(
                session, user_id=user.id, feed_type=FeedType.PERSONALIZED, limit=1
            )
            session.commit()
        assert first.page.items
        assert any(
            reason.startswith("dssm_recall_failed") for reason in first.trace.fallback_reasons
        )
        assert second.trace.cache_status == "process_fallback_hit"
        with pytest.raises(ValueError, match="invalid"):
            RecommendationService._decode_cached_retrieval(
                {
                    "candidates": [
                        {"item_id": [], "source": "x", "raw_score": True, "reason": "x"}
                    ],
                    "fallback_reasons": [],
                }
            )
    finally:
        engine.dispose()


def test_schema_invalid_cache_is_invalidated_and_reloaded_from_authority() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            add_items(session)
            user = add_user(session, username="poison-cache-user")
            session.commit()
        backend = PoisonOnceBackend()
        service = build_service(backend=backend)
        with factory() as session:
            result = service.get_page(session, user_id=user.id, feed_type=FeedType.POPULAR, limit=2)
            session.commit()
        assert result.page.items
        assert result.trace.cache_status == "corrupt_reloaded"
        assert "invalid_cached_retrieval_reloaded" in result.trace.fallback_reasons
        assert backend.poisoned is False
    finally:
        engine.dispose()


def test_offline_item_overrides_active_promotion() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            item_ids = add_items(session)
            user = add_user(session, username="promotion-user")
            operator = add_user(session, username="promotion-operator", role=Role.OPERATOR)
            batch = OperationBatch(
                batch_id=uuid.uuid4(),
                operator_id=operator.id,
                operator_role=Role.OPERATOR,
                operation_type=OperationType.PROMOTE,
                targets=[item_ids[0]],
                reason="fixture promotion",
                expected_state_version=0,
                status=OperationBatchStatus.SUCCEEDED,
                scope_type=ScopeType.ALL,
                scope_value=None,
                priority=10,
                target_position=0,
                starts_at=NOW - timedelta(minutes=1),
                ends_at=NOW + timedelta(minutes=5),
                started_at=NOW,
                completed_at=NOW,
                created_at=NOW,
                result={"applied": 1},
            )
            session.add(batch)
            session.flush()
            session.add(
                PromotionRule(
                    item_id=item_ids[0],
                    created_by=operator.id,
                    scope_type=ScopeType.ALL,
                    scope_value=None,
                    starts_at=batch.starts_at,
                    ends_at=batch.ends_at,
                    priority=10,
                    target_position=0,
                    reason="fixture promotion",
                    status=PromotionStatus.ACTIVE,
                    operation_batch_id=batch.batch_id,
                )
            )
            session.get(Item, item_ids[0]).online_status = OnlineStatus.OFFLINE
            session.commit()
        service = build_service()
        with factory() as session:
            result = service.get_page(session, user_id=user.id, feed_type=FeedType.POPULAR, limit=5)
            session.commit()
        assert item_ids[0] not in {row.item_id for row in result.page.items}
        assert all(row.source != "promotion" for row in result.page.items)
    finally:
        engine.dispose()


def test_page_failure_rolls_back_snapshot_request_exposure_and_impression() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    try:
        with factory() as session:
            add_items(session)
            user = add_user(session, username="rollback-user")
            session.commit()
        service = build_service(snapshot_service=FailingPageSnapshotService())
        with factory() as session:
            with pytest.raises(RuntimeError, match="injected"):
                service.get_page(session, user_id=user.id, feed_type=FeedType.POPULAR, limit=2)
            session.rollback()
        with factory() as session:
            assert session.scalar(select(func.count(RecommendationSnapshot.snapshot_id))) == 0
            assert session.scalar(select(func.count(RecommendationRequest.request_id))) == 0
            assert session.scalar(select(func.count(Exposure.id))) == 0
            assert session.scalar(select(func.count(Event.id))) == 0
    finally:
        engine.dispose()
