from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import distinct, func, or_, select, union
from sqlalchemy.orm import Session

from apps.api.app.auth.errors import ApiError
from apps.api.app.db.models import (
    AccountStatus,
    Event,
    EventType,
    Exposure,
    FeedType,
    Item,
    ModelStatus,
    ModelVersion,
    OnlineStatus,
    RecommendationRequest,
    RecommendationSnapshot,
    RecommendationSnapshotItem,
    Role,
    User,
    UserProfile,
)
from apps.api.app.events.service import profile_response

from .schemas import (
    DashboardBucket,
    DashboardFeedDiagnostics,
    DashboardOverview,
    FeedItemResponse,
    HotItem,
    PersistedEventResponse,
    RecommendationRequestDebugResponse,
    UserDebugResponse,
)


def require_window(from_utc: datetime, to_utc: datetime) -> tuple[datetime, datetime]:
    if from_utc.tzinfo is None or to_utc.tzinfo is None:
        raise ApiError(422, "timezone_required", "Dashboard timestamps must include an offset")
    start = from_utc.astimezone(UTC)
    end = to_utc.astimezone(UTC)
    if end <= start:
        raise ApiError(422, "invalid_time_window", "to_utc must be after from_utc")
    return start, end


def _count(session: Session, statement: Any) -> int:
    return int(session.scalar(statement) or 0)


def _db_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class DashboardQueryService:
    def overview(
        self, session: Session, *, from_utc: datetime, to_utc: datetime
    ) -> DashboardOverview:
        start, end = require_window(from_utc, to_utc)
        total_users = _count(
            session,
            select(func.count(User.id)).where(
                User.role == Role.USER, User.status == AccountStatus.ENABLED
            ),
        )
        request_users = select(RecommendationRequest.user_id.label("user_id")).where(
            RecommendationRequest.created_at >= start,
            RecommendationRequest.created_at < end,
        )
        event_users = select(Event.user_id.label("user_id")).where(
            Event.server_timestamp >= start,
            Event.server_timestamp < end,
            Event.event_type != EventType.IMPRESSION,
        )
        active_subquery = union(request_users, event_users).subquery()
        active_users = _count(
            session,
            select(func.count(distinct(active_subquery.c.user_id)))
            .select_from(active_subquery)
            .join(User, User.id == active_subquery.c.user_id)
            .where(User.role == Role.USER),
        )
        requests = _count(
            session,
            select(func.count(RecommendationRequest.request_id)).where(
                RecommendationRequest.created_at >= start,
                RecommendationRequest.created_at < end,
            ),
        )
        exposures = _count(
            session,
            select(func.count(Exposure.id)).where(
                Exposure.exposed_at >= start, Exposure.exposed_at < end
            ),
        )
        event_counts = dict(
            session.execute(
                select(Event.event_type, func.count(Event.id))
                .where(Event.server_timestamp >= start, Event.server_timestamp < end)
                .group_by(Event.event_type)
            ).all()
        )
        dwell_total = _count(
            session,
            select(func.coalesce(func.sum(Event.duration_ms), 0)).where(
                Event.server_timestamp >= start,
                Event.server_timestamp < end,
                Event.event_type == EventType.DWELL,
            ),
        )
        active_model = session.scalar(
            select(ModelVersion.model_version).where(ModelVersion.status == ModelStatus.ACTIVE)
        )
        clicks = int(event_counts.get(EventType.CLICK, 0))
        offline_item_count = _count(
            session,
            select(func.count(Item.id)).where(Item.online_status == OnlineStatus.OFFLINE),
        )
        return DashboardOverview(
            from_utc=start,
            to_utc=end,
            total_users=total_users,
            active_users=active_users,
            requests=requests,
            exposures=exposures,
            clicks=clicks,
            likes=int(event_counts.get(EventType.LIKE, 0)),
            shares=int(event_counts.get(EventType.SHARE, 0)),
            revisits=int(event_counts.get(EventType.REVISIT, 0)),
            dwell_ms_total=dwell_total,
            offline_item_count=offline_item_count,
            ctr=clicks / exposures if exposures else 0.0,
            zero_denominator=exposures == 0,
            active_model_version=active_model,
        )

    def timeseries(
        self,
        session: Session,
        *,
        from_utc: datetime,
        to_utc: datetime,
        feed_type: FeedType | None = None,
    ) -> list[DashboardBucket]:
        start, end = require_window(from_utc, to_utc)
        request_bucket = func.date_trunc("hour", RecommendationRequest.created_at)
        request_query = (
            select(
                request_bucket.label("bucket"),
                RecommendationSnapshot.feed_type,
                func.count(RecommendationRequest.request_id).label("count"),
            )
            .join(
                RecommendationSnapshot,
                RecommendationSnapshot.snapshot_id == RecommendationRequest.snapshot_id,
            )
            .where(
                RecommendationRequest.created_at >= start,
                RecommendationRequest.created_at < end,
            )
            .group_by(request_bucket, RecommendationSnapshot.feed_type)
        )
        exposure_bucket = func.date_trunc("hour", Exposure.exposed_at)
        exposure_query = (
            select(
                exposure_bucket.label("bucket"),
                RecommendationSnapshot.feed_type,
                func.count(Exposure.id).label("count"),
            )
            .join(
                RecommendationSnapshot,
                RecommendationSnapshot.snapshot_id == Exposure.snapshot_id,
            )
            .where(Exposure.exposed_at >= start, Exposure.exposed_at < end)
            .group_by(exposure_bucket, RecommendationSnapshot.feed_type)
        )
        event_bucket = func.date_trunc("hour", Event.server_timestamp)
        event_query = (
            select(
                event_bucket.label("bucket"),
                Event.feed_type,
                Event.event_type,
                func.count(Event.id).label("count"),
                func.coalesce(func.sum(Event.duration_ms), 0).label("duration"),
            )
            .where(Event.server_timestamp >= start, Event.server_timestamp < end)
            .group_by(event_bucket, Event.feed_type, Event.event_type)
        )
        request_active_bucket = func.date_trunc("hour", RecommendationRequest.created_at)
        request_active_query = (
            select(
                request_active_bucket.label("bucket"),
                RecommendationSnapshot.feed_type.label("feed_type"),
                RecommendationRequest.user_id.label("user_id"),
            )
            .join(
                RecommendationSnapshot,
                RecommendationSnapshot.snapshot_id == RecommendationRequest.snapshot_id,
            )
            .join(User, User.id == RecommendationRequest.user_id)
            .where(
                RecommendationRequest.created_at >= start,
                RecommendationRequest.created_at < end,
                User.role == Role.USER,
            )
        )
        event_active_bucket = func.date_trunc("hour", Event.server_timestamp)
        event_active_query = (
            select(
                event_active_bucket.label("bucket"),
                Event.feed_type.label("feed_type"),
                Event.user_id.label("user_id"),
            )
            .join(User, User.id == Event.user_id)
            .where(
                Event.server_timestamp >= start,
                Event.server_timestamp < end,
                User.role == Role.USER,
                Event.event_type != EventType.IMPRESSION,
            )
        )
        if feed_type is not None:
            request_query = request_query.where(RecommendationSnapshot.feed_type == feed_type)
            exposure_query = exposure_query.where(RecommendationSnapshot.feed_type == feed_type)
            event_query = event_query.where(Event.feed_type == feed_type)
            request_active_query = request_active_query.where(
                RecommendationSnapshot.feed_type == feed_type
            )
            event_active_query = event_active_query.where(Event.feed_type == feed_type)

        values: dict[tuple[datetime, FeedType], dict[str, float]] = defaultdict(dict)
        for bucket, feed, count in session.execute(request_query):
            values[(_db_datetime(bucket), FeedType(feed))]["request_count"] = int(count)
        for bucket, feed, count in session.execute(exposure_query):
            values[(_db_datetime(bucket), FeedType(feed))]["exposure_count"] = int(count)
        dwell_counts: dict[tuple[datetime, FeedType], int] = defaultdict(int)
        for bucket, feed, kind, count, duration in session.execute(event_query):
            key = (_db_datetime(bucket), FeedType(feed))
            if kind == EventType.DWELL:
                values[key]["dwell_ms_total"] = int(duration)
                dwell_counts[key] = int(count)
            elif kind in {
                EventType.CLICK,
                EventType.LIKE,
                EventType.SHARE,
                EventType.REVISIT,
            }:
                values[key][f"{kind.value}_count"] = int(count)
        active: dict[tuple[datetime, FeedType], set[uuid.UUID]] = defaultdict(set)
        active_query = union(request_active_query, event_active_query)
        for bucket, feed, user_id in session.execute(active_query):
            active[(_db_datetime(bucket), FeedType(feed))].add(user_id)

        rows: list[DashboardBucket] = []
        for (bucket, feed), metrics in sorted(
            values.items(), key=lambda item: (item[0][0], item[0][1].value)
        ):
            bucket_start = max(bucket, start)
            bucket_end = min(bucket + timedelta(hours=1), end)
            exposure_count = int(metrics.get("exposure_count", 0))
            click_count = int(metrics.get("click_count", 0))
            dwell_count = dwell_counts[(bucket, feed)]
            dwell_total = int(metrics.get("dwell_ms_total", 0))
            rows.append(
                DashboardBucket(
                    bucket_start_utc=bucket_start,
                    bucket_end_utc=bucket_end,
                    feed_type=feed.value,
                    request_count=int(metrics.get("request_count", 0)),
                    exposure_count=exposure_count,
                    click_count=click_count,
                    like_count=int(metrics.get("like_count", 0)),
                    share_count=int(metrics.get("share_count", 0)),
                    revisit_count=int(metrics.get("revisit_count", 0)),
                    dwell_ms_total=dwell_total,
                    dwell_ms_avg=dwell_total / dwell_count if dwell_count else 0.0,
                    ctr=click_count / exposure_count if exposure_count else 0.0,
                    active_user_count=len(active[(bucket, feed)]),
                )
            )
        return rows

    def feeds(
        self, session: Session, *, from_utc: datetime, to_utc: datetime
    ) -> DashboardFeedDiagnostics:
        start, end = require_window(from_utc, to_utc)
        detailed = self.timeseries(session, from_utc=start, to_utc=end)
        totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in detailed:
            target = totals[row.feed_type]
            for key in (
                "request_count",
                "exposure_count",
                "click_count",
                "like_count",
                "share_count",
                "revisit_count",
                "dwell_ms_total",
            ):
                target[key] += float(getattr(row, key))
        request_users = (
            select(
                RecommendationSnapshot.feed_type.label("feed_type"),
                RecommendationRequest.user_id.label("user_id"),
            )
            .join(
                RecommendationSnapshot,
                RecommendationSnapshot.snapshot_id == RecommendationRequest.snapshot_id,
            )
            .join(User, User.id == RecommendationRequest.user_id)
            .where(
                RecommendationRequest.created_at >= start,
                RecommendationRequest.created_at < end,
                User.role == Role.USER,
            )
        )
        event_users = (
            select(Event.feed_type.label("feed_type"), Event.user_id.label("user_id"))
            .join(User, User.id == Event.user_id)
            .where(
                Event.server_timestamp >= start,
                Event.server_timestamp < end,
                Event.event_type != EventType.IMPRESSION,
                User.role == Role.USER,
            )
        )
        users: dict[str, set[uuid.UUID]] = defaultdict(set)
        for feed, user_id in session.execute(union(request_users, event_users)):
            users[feed.value if isinstance(feed, FeedType) else str(feed)].add(user_id)
        rows: list[DashboardBucket] = []
        for feed in sorted(totals):
            metrics = totals[feed]
            dwell_count = _count(
                session,
                select(func.count(Event.id)).where(
                    Event.server_timestamp >= start,
                    Event.server_timestamp < end,
                    Event.feed_type == FeedType(feed),
                    Event.event_type == EventType.DWELL,
                ),
            )
            exposures = int(metrics["exposure_count"])
            clicks = int(metrics["click_count"])
            dwell_total = int(metrics["dwell_ms_total"])
            rows.append(
                DashboardBucket(
                    bucket_start_utc=start,
                    bucket_end_utc=end,
                    feed_type=feed,
                    request_count=int(metrics["request_count"]),
                    exposure_count=exposures,
                    click_count=clicks,
                    like_count=int(metrics["like_count"]),
                    share_count=int(metrics["share_count"]),
                    revisit_count=int(metrics["revisit_count"]),
                    dwell_ms_total=dwell_total,
                    dwell_ms_avg=dwell_total / dwell_count if dwell_count else 0.0,
                    ctr=clicks / exposures if exposures else 0.0,
                    active_user_count=len(users[feed]),
                )
            )
        total_requests = sum(row.request_count for row in rows)
        feed_share = {
            feed.value: (
                next((row.request_count for row in rows if row.feed_type == feed.value), 0)
                / total_requests
                if total_requests
                else 0.0
            )
            for feed in FeedType
        }
        return DashboardFeedDiagnostics(
            from_utc=start,
            to_utc=end,
            feeds=rows,
            feed_share=feed_share,
        )

    def hot_items(
        self, session: Session, *, from_utc: datetime, to_utc: datetime, limit: int = 20
    ) -> list[HotItem]:
        start, end = require_window(from_utc, to_utc)
        exposure_count = func.count(distinct(Exposure.id))
        click_count = func.count(distinct(Event.id)).filter(Event.event_type == EventType.CLICK)
        like_count = func.count(distinct(Event.id)).filter(Event.event_type == EventType.LIKE)
        rows = session.execute(
            select(
                Item.id,
                Item.title,
                exposure_count.label("exposures"),
                click_count.label("clicks"),
                like_count.label("likes"),
            )
            .outerjoin(
                Exposure,
                (Exposure.item_id == Item.id)
                & (Exposure.exposed_at >= start)
                & (Exposure.exposed_at < end),
            )
            .outerjoin(
                Event,
                (Event.item_id == Item.id)
                & (Event.server_timestamp >= start)
                & (Event.server_timestamp < end),
            )
            .group_by(Item.id, Item.title)
            .having(or_(exposure_count > 0, click_count > 0, like_count > 0))
            .order_by(click_count.desc(), like_count.desc(), exposure_count.desc(), Item.id)
            .limit(limit)
        )
        return [
            HotItem(
                item_id=item_id,
                title=title,
                exposure_count=int(exposures),
                click_count=int(clicks),
                like_count=int(likes),
            )
            for item_id, title, exposures, clicks, likes in rows
        ]

    def user_debug(self, session: Session, user_id: uuid.UUID) -> UserDebugResponse:
        user = session.get(User, user_id)
        profile = session.get(UserProfile, user_id)
        if user is None or profile is None:
            raise ApiError(404, "user_not_found", "User or profile does not exist")
        requests = list(
            session.scalars(
                select(RecommendationRequest.request_id)
                .where(RecommendationRequest.user_id == user_id)
                .order_by(RecommendationRequest.created_at.desc())
                .limit(20)
            )
        )
        return UserDebugResponse(
            user_id=user_id,
            profile=profile_response(profile),
            recent_request_ids=requests,
        )

    def request_debug(
        self, session: Session, request_id: uuid.UUID
    ) -> RecommendationRequestDebugResponse:
        page_request = session.get(RecommendationRequest, request_id)
        if page_request is None:
            raise ApiError(404, "request_not_found", "Recommendation request does not exist")
        snapshot_items = list(
            session.scalars(
                select(RecommendationSnapshotItem)
                .where(RecommendationSnapshotItem.snapshot_id == page_request.snapshot_id)
                .order_by(RecommendationSnapshotItem.snapshot_position)
            )
        )
        item_map = {
            item.id: item
            for item in session.scalars(
                select(Item).where(Item.id.in_([row.item_id for row in snapshot_items]))
            )
        }
        snapshot = session.get(RecommendationSnapshot, page_request.snapshot_id)
        assert snapshot is not None
        exposures = list(
            session.scalars(
                select(Exposure)
                .where(Exposure.request_id == request_id)
                .order_by(Exposure.position)
            )
        )
        ranked = [
            FeedItemResponse(
                item_id=exposure.item_id,
                title=item_map[exposure.item_id].title,
                cover=item_map[exposure.item_id].cover_ref,
                position=exposure.position,
                source=exposure.source,
                score=next(
                    row.normalized_score
                    for row in snapshot_items
                    if row.item_id == exposure.item_id
                ),
                reason="exposed",
                model_version=exposure.model_version,
            )
            for exposure in exposures
        ]
        events = list(
            session.scalars(select(Event).where(Event.request_id == request_id).order_by(Event.id))
        )
        return RecommendationRequestDebugResponse(
            request_id=request_id,
            candidate_item_ids=[row.item_id for row in snapshot_items],
            filtered_item_ids=[
                row.item_id for row in snapshot_items if row.filter_reason is not None
            ],
            ranked_items=ranked,
            events=[
                PersistedEventResponse(
                    event_id=event.event_id,
                    request_id=event.request_id,
                    item_id=event.item_id,
                    position=event.position,
                    event_type=event.event_type.value,
                    client_timestamp=event.client_timestamp,
                    server_timestamp=event.server_timestamp,
                    duration_ms=event.duration_ms,
                    payload=event.payload,
                )
                for event in events
            ],
        )
