from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.app.cache import CacheAuthority, VersionedCache
from apps.api.app.db.base import ensure_utc, utc_now
from apps.api.app.db.models import (
    AccountStatus,
    Event,
    EventType,
    FeedType,
    Item,
    ModelStatus,
    ModelVersion,
    OnlineStatus,
    Operation,
    RecommendationSnapshot,
    RecommendationSnapshotItem,
    User,
    UserProfile,
)
from apps.api.app.events.service import PageExposure, SnapshotCandidate, SnapshotService
from apps.api.app.operations.service import OperationService

from .cursor import CursorCodec, CursorState
from .domain import RankedCandidate, RecallCandidate, RecommendationTrace
from .ranking import (
    PromotionPlacement,
    apply_promotions,
    merge_recall,
    mmr_rank,
    topic_deduplicate,
)
from .retrieval import (
    CatalogItem,
    rank_candidates,
    retrieve_candidates,
)
from .schemas import FeedItem, FeedPage

LOGGER = logging.getLogger("uvicorn.error")
FALLBACK_MODEL_VERSION = "fallback-no-active-model"


@dataclass(frozen=True, slots=True)
class RecommendationConfig:
    candidate_pool_size: int = 200
    snapshot_ttl: timedelta = timedelta(minutes=15)
    default_page_size: int = 20
    topic_dedup_enabled: bool = True
    topic_max_per_group: int = 1
    mmr_enabled: bool = True
    mmr_lambda: float = 0.75

    def __post_init__(self) -> None:
        if self.candidate_pool_size < 1:
            raise ValueError("candidate_pool_size must be positive")
        if self.snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        if not 1 <= self.default_page_size <= 100:
            raise ValueError("default_page_size must be in [1, 100]")
        if self.topic_max_per_group < 1:
            raise ValueError("topic_max_per_group must be positive")
        if not 0.0 <= self.mmr_lambda <= 1.0:
            raise ValueError("mmr_lambda must be between zero and one")


@dataclass(frozen=True, slots=True)
class FeedResult:
    page: FeedPage
    trace: RecommendationTrace


@dataclass(frozen=True, slots=True)
class _AuthorityState:
    user: User
    profile: UserProfile
    active_model_version: str
    active_data_version: str | None
    active_data_manifest_checksum: str | None
    operations_generation: int


class RecommendationService:
    """Build immutable snapshots and persist each delivered page without committing."""

    def __init__(
        self,
        *,
        model_provider: Callable[[], tuple[str | None, object | None]],
        cache: VersionedCache,
        cursor_codec: CursorCodec,
        snapshot_service: SnapshotService | None = None,
        operation_service: OperationService | None = None,
        config: RecommendationConfig | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model_provider = model_provider
        self.cache = cache
        self.cursor_codec = cursor_codec
        self.snapshot_service = snapshot_service or SnapshotService()
        self.operation_service = operation_service or OperationService()
        self.config = config or RecommendationConfig()
        self.clock = clock
        self.monotonic = monotonic

    def get_page(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        feed_type: FeedType,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> FeedResult:
        page_limit = self.config.default_page_size if limit is None else limit
        if isinstance(page_limit, bool) or not isinstance(page_limit, int):
            raise ValueError("feed limit must be an integer in [1, 100]")
        if not 1 <= page_limit <= 100:
            raise ValueError("feed limit must be in [1, 100]")
        started = self.monotonic()
        now = self.clock().astimezone(UTC)
        if cursor is not None:
            state = self.cursor_codec.decode(
                cursor,
                user_id=user_id,
                feed_type=feed_type.value,
                now=now,
            )
            trace = RecommendationTrace(
                snapshot_id=state.snapshot_id,
                user_id=user_id,
                feed_type=feed_type.value,
                cache_status="cursor_snapshot",
            )
            return self._page_from_snapshot(
                session,
                state=state,
                limit=page_limit,
                trace=trace,
                started=started,
                now=now,
            )

        return self._new_snapshot(
            session,
            user_id=user_id,
            feed_type=feed_type,
            limit=page_limit,
            started=started,
            now=now,
        )

    def _new_snapshot(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        feed_type: FeedType,
        limit: int,
        started: float,
        now: datetime,
    ) -> FeedResult:
        slot_version, resource = self.model_provider()
        authority_state = self._authority_state(session, user_id=user_id)
        model_version = authority_state.active_model_version
        trace = RecommendationTrace(
            user_id=user_id,
            feed_type=feed_type.value,
            model_version=model_version,
        )
        resource_aligned = (
            resource is not None
            and slot_version == model_version
            and getattr(resource, "model_version", None) == model_version
            and getattr(resource, "data_version", None) == authority_state.active_data_version
            and getattr(resource, "data_manifest_checksum", None)
            == authority_state.active_data_manifest_checksum
            and getattr(resource, "verified_status", None) == "checksum_verified"
        )
        if resource_aligned:
            bundle = getattr(resource, "bundle", None)
            item_item_index = getattr(resource, "item_item_index", None)
            source_histories = getattr(resource, "source_histories", {})
        else:
            if model_version != FALLBACK_MODEL_VERSION:
                trace.add_fallback("runtime_serving_resource_not_aligned_with_database_active")
            bundle = None
            item_item_index = None
            source_histories = {}

        seed = self._snapshot_seed(
            user_id=user_id,
            feed_type=feed_type,
            profile_version=authority_state.profile.profile_version,
            model_version=model_version,
            operations_generation=authority_state.operations_generation,
        )
        before_metrics = self.cache.metrics.snapshot()
        resource = (
            f"natural-feed:{user_id}:{feed_type.value}:{self.config.candidate_pool_size}:"
            f"topic-{int(self.config.topic_dedup_enabled)}:mmr-{int(self.config.mmr_enabled)}:"
            f"data-{authority_state.active_data_version}:"
            f"checksum-{authority_state.active_data_manifest_checksum}:"
            f"runtime-{int(resource_aligned)}"
        )

        def authority() -> CacheAuthority:
            current = self._authority_state(session, user_id=user_id)
            self._assert_same_generation(authority_state, current)
            return CacheAuthority(
                profile_version=current.profile.profile_version,
                active_model_version=current.active_model_version,
                operations_generation=current.operations_generation,
                permission_generation=0,
                allowed=current.user.status == AccountStatus.ENABLED,
                online=True,
            )

        online_history = self._recent_item_ids(authority_state.profile)
        official_history = source_histories.get(authority_state.user.source_user_id, ())
        recall_seed_ids = list(dict.fromkeys([*online_history, *official_history]))

        def loader() -> dict[str, object]:
            catalog = self._catalog(session)
            retrieval = retrieve_candidates(
                feed_type=feed_type.value,
                catalog=list(catalog.values()),
                bundle=bundle,
                source_user_id=authority_state.user.source_user_id,
                profile_title_preferences=authority_state.profile.title_preferences,
                recent_item_ids=recall_seed_ids,
                item_item_index=item_item_index,
                seed=seed,
                top_n=self.config.candidate_pool_size,
            )
            return {
                "candidates": [candidate.as_dict() for candidate in retrieval.candidates],
                "fallback_reasons": list(retrieval.fallback_reasons),
            }

        cached = self.cache.get_or_load(resource=resource, authority=authority, loader=loader)
        after_metrics = self.cache.metrics.snapshot()
        trace.cache_status = self._cache_status(before_metrics, after_metrics)
        try:
            recall, cached_fallbacks = self._decode_cached_retrieval(cached)
        except ValueError:
            # A syntactically valid but schema-invalid Redis value is never coerced.
            # Remove only the current versioned key, then perform one authoritative load.
            self.cache.invalidate(resource=resource, authority=authority())
            cached = self.cache.get_or_load(resource=resource, authority=authority, loader=loader)
            recall, cached_fallbacks = self._decode_cached_retrieval(cached)
            trace.cache_status = "corrupt_reloaded"
            trace.add_fallback("invalid_cached_retrieval_reloaded")
        for reason in cached_fallbacks:
            trace.add_fallback(str(reason))
        trace.source_counts = dict(Counter(candidate.source for candidate in recall))

        # PostgreSQL is re-read after every cache result. Cached candidate IDs and item
        # metadata never authorize delivery or revive an offline item.
        catalog = self._catalog(session)
        online_ids = set(catalog)
        pre_online_count = len(recall)
        recall = [candidate for candidate in recall if candidate.item_id in online_ids]
        trace.filter_counts["offline_after_cache"] = pre_online_count - len(recall)

        official_viewed = set(official_history) if feed_type == FeedType.PERSONALIZED else set()
        before_official_history = len(recall)
        recall = [candidate for candidate in recall if candidate.item_id not in official_viewed]
        trace.filter_counts["official_train_history"] = before_official_history - len(recall)

        viewed, not_interested = self._behavior_filters(session, user_id=user_id)
        before_behavior = len(recall)
        recall = [
            candidate
            for candidate in recall
            if candidate.item_id not in viewed and candidate.item_id not in not_interested
        ]
        trace.filter_counts["viewed_or_not_interested"] = before_behavior - len(recall)

        merged = merge_recall(recall)
        positive_titles = self._positive_history_titles(session, profile=authority_state.profile)
        ranking = rank_candidates(
            merged=merged,
            catalog=catalog,
            bundle=bundle,
            source_user_id=authority_state.user.source_user_id,
            positive_history_titles=positive_titles,
            profile_activity_count=len(authority_state.profile.recent_interactions),
        )
        for reason in ranking.fallback_reasons:
            trace.add_fallback(reason)
        natural = list(ranking.candidates)
        if self.config.topic_dedup_enabled:
            natural, removed = topic_deduplicate(
                natural, max_per_topic=self.config.topic_max_per_group
            )
            trace.filter_counts["derived_title_topic_dedup"] = len(removed)
        if self.config.mmr_enabled:
            natural, steps = mmr_rank(
                natural,
                vectors=ranking.title_vectors,
                lambda_value=self.config.mmr_lambda,
            )
            trace.mmr_steps = steps
            for step in steps:
                if step.fallback_reason:
                    trace.add_fallback(step.fallback_reason)

        promotions = self.operation_service.active_promotions(
            session, now=now, user_id=user_id, feed_type=feed_type
        )
        promotion_items = self._catalog(session, item_ids={rule.item_id for rule in promotions})
        promoted_candidates = {
            item_id: self._promotion_candidate(item, index=len(natural) + index)
            for index, (item_id, item) in enumerate(sorted(promotion_items.items()))
        }
        final = apply_promotions(
            natural,
            placements=[
                PromotionPlacement(
                    rule_id=rule.id,
                    item_id=rule.item_id,
                    priority=rule.priority,
                    target_position=rule.target_position,
                    reason=rule.reason,
                )
                for rule in promotions
            ],
            promoted_candidates=promoted_candidates,
        )

        self._assert_same_generation(
            authority_state,
            self._authority_state(session, user_id=user_id),
        )

        expires_at = now + self.config.snapshot_ttl
        snapshot = self.snapshot_service.create_snapshot(
            session,
            user_id=user_id,
            feed_type=feed_type,
            model_version=model_version,
            snapshot_seed=seed,
            expires_at=expires_at,
            candidates=[
                SnapshotCandidate(
                    item_id=candidate.item_id,
                    source=candidate.source,
                    raw_score=candidate.score,
                    normalized_score=candidate.normalized_score,
                    snapshot_position=index,
                    filter_reason=candidate.reason[:255],
                    promotion_rule_id=candidate.promotion_rule_id,
                )
                for index, candidate in enumerate(final)
            ],
            now=now,
        )
        trace.snapshot_id = snapshot.snapshot_id
        state = CursorState(
            snapshot_id=snapshot.snapshot_id,
            user_id=user_id,
            feed_type=feed_type.value,
            offset=0,
            scan_offset=0,
            expires_at=expires_at,
        )
        return self._page_from_snapshot(
            session,
            state=state,
            limit=limit,
            trace=trace,
            started=started,
            now=now,
        )

    def _page_from_snapshot(
        self,
        session: Session,
        *,
        state: CursorState,
        limit: int,
        trace: RecommendationTrace,
        started: float,
        now: datetime,
    ) -> FeedResult:
        snapshot = session.get(RecommendationSnapshot, state.snapshot_id)
        if snapshot is None or snapshot.user_id != state.user_id:
            raise ValueError("snapshot does not belong to the authenticated user")
        if snapshot.feed_type.value != state.feed_type:
            raise ValueError("snapshot feed does not match cursor")
        if ensure_utc(snapshot.expires_at) <= now:
            raise ValueError("snapshot is expired")
        trace.model_version = snapshot.model_version

        rows = list(
            session.execute(
                select(RecommendationSnapshotItem, Item)
                .join(Item, Item.id == RecommendationSnapshotItem.item_id)
                .where(
                    RecommendationSnapshotItem.snapshot_id == state.snapshot_id,
                    RecommendationSnapshotItem.snapshot_position >= state.scan_offset,
                )
                .order_by(RecommendationSnapshotItem.snapshot_position)
            )
        )
        selected: list[tuple[RecommendationSnapshotItem, Item]] = []
        next_scan_offset = state.scan_offset
        selected_row_index = -1
        for row_index, (snapshot_item, item) in enumerate(rows):
            next_scan_offset = snapshot_item.snapshot_position + 1
            if item.online_status != OnlineStatus.ONLINE:
                trace.filter_counts["offline_during_cursor"] = (
                    trace.filter_counts.get("offline_during_cursor", 0) + 1
                )
                continue
            selected.append((snapshot_item, item))
            selected_row_index = row_index
            if len(selected) == limit:
                break

        remaining = rows[selected_row_index + 1 :] if selected_row_index >= 0 else rows
        has_more = any(
            item.online_status == OnlineStatus.ONLINE for _snapshot_item, item in remaining
        )
        page_entries = [
            PageExposure(
                item_id=snapshot_item.item_id,
                position=state.offset + index,
                source=snapshot_item.source,
            )
            for index, (snapshot_item, _item) in enumerate(selected)
        ]

        # Recheck user authority immediately before the transactional page write. The
        # SnapshotService then independently rechecks every selected item's online state.
        self._assert_user_allowed(session, state.user_id)
        latency_ms = max(0, int((self.monotonic() - started) * 1000))
        request = self.snapshot_service.record_page(
            session,
            snapshot_id=state.snapshot_id,
            user_id=state.user_id,
            offset=state.offset,
            limit=limit,
            latency_ms=latency_ms,
            page=page_entries,
            now=now,
        )
        next_cursor = None
        if has_more:
            next_cursor = self.cursor_codec.encode(
                CursorState(
                    snapshot_id=state.snapshot_id,
                    user_id=state.user_id,
                    feed_type=state.feed_type,
                    offset=state.offset + len(selected),
                    scan_offset=next_scan_offset,
                    expires_at=ensure_utc(snapshot.expires_at),
                )
            )
        items = [
            FeedItem(
                item_id=snapshot_item.item_id,
                title=item.title,
                cover=item.cover_ref,
                position=state.offset + index,
                source=snapshot_item.source,
                score=snapshot_item.raw_score,
                reason=snapshot_item.filter_reason or snapshot_item.source,
                model_version=snapshot.model_version,
            )
            for index, (snapshot_item, item) in enumerate(selected)
        ]
        page = FeedPage(
            snapshot_id=state.snapshot_id,
            request_id=request.request_id,
            model_version=snapshot.model_version,
            items=items,
            next_cursor=next_cursor,
        )
        trace.request_id = request.request_id
        trace.latency_ms = latency_ms
        LOGGER.info(
            json.dumps(
                {"event": "recommendation_page", **trace.as_dict()},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return FeedResult(page=page, trace=trace)

    @staticmethod
    def _snapshot_seed(
        *,
        user_id: uuid.UUID,
        feed_type: FeedType,
        profile_version: int,
        model_version: str,
        operations_generation: int,
    ) -> int:
        value = (
            f"{user_id}:{feed_type.value}:{profile_version}:{model_version}:{operations_generation}"
        )
        return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big") & (
            2**63 - 1
        )

    @staticmethod
    def _cache_status(before: dict[str, int | float], after: dict[str, int | float]) -> str:
        if int(after["process_fallback_hits"]) > int(before["process_fallback_hits"]):
            return "process_fallback_hit"
        if int(after["hits"]) > int(before["hits"]):
            return "hit"
        if int(after["misses"]) > int(before["misses"]):
            return "miss"
        return "unknown"

    @staticmethod
    def _decode_cached_retrieval(
        value: object,
    ) -> tuple[list[RecallCandidate], list[str]]:
        if not isinstance(value, dict) or set(value) != {"candidates", "fallback_reasons"}:
            raise ValueError("cached retrieval payload has an invalid shape")
        candidate_rows = value["candidates"]
        fallback_rows = value["fallback_reasons"]
        if not isinstance(candidate_rows, list) or not isinstance(fallback_rows, list):
            raise ValueError("cached retrieval payload fields must be arrays")
        if any(not isinstance(reason, str) or not reason for reason in fallback_rows):
            raise ValueError("cached retrieval fallback reasons are invalid")
        return [RecallCandidate.from_dict(row) for row in candidate_rows], list(fallback_rows)

    @staticmethod
    def _catalog(session: Session, *, item_ids: set[str] | None = None) -> dict[str, CatalogItem]:
        query = select(Item).where(Item.online_status == OnlineStatus.ONLINE)
        if item_ids is not None:
            if not item_ids:
                return {}
            query = query.where(Item.id.in_(item_ids))
        items = list(session.scalars(query.order_by(Item.id)))
        return {
            item.id: CatalogItem(
                item_id=item.id,
                title=item.title,
                cover=item.cover_ref,
                likes=int(item.likes_snapshot or 0),
                views=int(item.views_snapshot or 0),
            )
            for item in items
        }

    @staticmethod
    def _authority_state(session: Session, *, user_id: uuid.UUID) -> _AuthorityState:
        user = session.scalar(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )
        profile = session.scalar(
            select(UserProfile)
            .where(UserProfile.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        if user is None or profile is None:
            raise PermissionError("recommendation user/profile does not exist")
        active_model = session.execute(
            select(
                ModelVersion.model_version,
                ModelVersion.data_version,
                ModelVersion.data_manifest_checksum,
            )
            .where(ModelVersion.status == ModelStatus.ACTIVE)
            .order_by(ModelVersion.model_version)
            .limit(1)
        ).one_or_none()
        generation = int(session.scalar(select(func.count(Operation.id))) or 0)
        return _AuthorityState(
            user=user,
            profile=profile,
            active_model_version=(
                active_model.model_version if active_model is not None else FALLBACK_MODEL_VERSION
            ),
            active_data_version=(active_model.data_version if active_model is not None else None),
            active_data_manifest_checksum=(
                active_model.data_manifest_checksum if active_model is not None else None
            ),
            operations_generation=generation,
        )

    @staticmethod
    def _assert_same_generation(expected: _AuthorityState, actual: _AuthorityState) -> None:
        expected_identity = (
            expected.user.id,
            expected.user.status,
            expected.profile.profile_version,
            expected.active_model_version,
            expected.active_data_version,
            expected.active_data_manifest_checksum,
            expected.operations_generation,
        )
        actual_identity = (
            actual.user.id,
            actual.user.status,
            actual.profile.profile_version,
            actual.active_model_version,
            actual.active_data_version,
            actual.active_data_manifest_checksum,
            actual.operations_generation,
        )
        if actual_identity != expected_identity:
            raise ValueError("feed authority changed while building the snapshot; retry")

    @staticmethod
    def _assert_user_allowed(session: Session, user_id: uuid.UUID) -> None:
        status = session.scalar(select(User.status).where(User.id == user_id))
        if status != AccountStatus.ENABLED:
            raise PermissionError("current database permissions deny feed delivery")

    @staticmethod
    def _recent_item_ids(profile: UserProfile) -> list[str]:
        return list(
            dict.fromkeys(
                str(row.get("item_id"))
                for row in profile.recent_interactions
                if isinstance(row, dict) and row.get("item_id")
            )
        )

    @staticmethod
    def _positive_history_titles(session: Session, *, profile: UserProfile) -> list[str]:
        positive_ids = [
            str(row.get("item_id"))
            for row in profile.recent_interactions
            if isinstance(row, dict)
            and row.get("item_id")
            and row.get("event_type") != EventType.NOT_INTERESTED.value
        ]
        if not positive_ids:
            return []
        titles = dict(
            session.execute(select(Item.id, Item.title).where(Item.id.in_(set(positive_ids)))).all()
        )
        return [titles[item_id] for item_id in positive_ids if item_id in titles]

    @staticmethod
    def _behavior_filters(session: Session, *, user_id: uuid.UUID) -> tuple[set[str], set[str]]:
        rows = session.execute(
            select(Event.item_id, Event.event_type).where(
                Event.user_id == user_id,
                Event.event_type.in_((EventType.IMPRESSION, EventType.NOT_INTERESTED)),
            )
        )
        viewed: set[str] = set()
        not_interested: set[str] = set()
        for item_id, event_type in rows:
            if event_type == EventType.IMPRESSION:
                viewed.add(item_id)
            elif event_type == EventType.NOT_INTERESTED:
                not_interested.add(item_id)
        return viewed, not_interested

    @staticmethod
    def _promotion_candidate(item: CatalogItem, *, index: int) -> RankedCandidate:
        return RankedCandidate(
            item_id=item.item_id,
            title=item.title,
            cover=item.cover,
            source="promotion",
            sources=("promotion",),
            raw_score=1.0,
            normalized_score=1.0,
            score=1.0,
            reason="active promotion",
            original_rank=index,
            title_topic=f"promotion:{item.item_id}",
        )
