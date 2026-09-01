from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.db.base import ensure_utc, utc_now
from apps.api.app.db.models import (
    Event,
    EventBatch,
    EventType,
    Exposure,
    FeedType,
    Item,
    OnlineStatus,
    RecommendationRequest,
    RecommendationSnapshot,
    RecommendationSnapshotItem,
    UserProfile,
)

from .schemas import (
    EventBatchRequest,
    EventBatchResponse,
    EventItemResult,
    EventRequest,
    UserProfileResponse,
)

CANONICAL_IMPRESSION_NAMESPACE = uuid.UUID("7bb478eb-0310-4530-b3e7-ce694a6bb8c7")
TITLE_PREFERENCE_LIMIT = 64
TITLE_TOKEN_LIMIT = 32
TITLE_BEHAVIOR_WEIGHTS = {
    "click": 1,
    "like": 3,
    "dwell": 1,
    "revisit": 2,
    "share": 3,
    "not_interested": -4,
}


def canonical_json(value: Any) -> bytes:
    def encode(item: Any) -> Any:
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(item, uuid.UUID):
            return str(item)
        if isinstance(item, dict):
            return {key: encode(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [encode(nested) for nested in item]
        return item

    return json.dumps(
        encode(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def event_fingerprint(user_id: uuid.UUID, event: EventRequest) -> str:
    return fingerprint({"user_id": user_id, **event.model_dump(mode="python")})


def canonical_impression_event_id(request_id: uuid.UUID, item_id: str, position: int) -> uuid.UUID:
    return uuid.uuid5(CANONICAL_IMPRESSION_NAMESPACE, f"{request_id}:{item_id}:{position}")


@dataclass(frozen=True)
class SnapshotCandidate:
    item_id: str
    source: str
    raw_score: float
    normalized_score: float
    snapshot_position: int
    filter_reason: str | None = None
    promotion_rule_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PageExposure:
    item_id: str
    position: int
    source: str


class SnapshotService:
    """Persistence boundary used by the recommendation owner; never commits."""

    def create_snapshot(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        feed_type: FeedType,
        model_version: str,
        snapshot_seed: int,
        expires_at: datetime,
        candidates: list[SnapshotCandidate],
        snapshot_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> RecommendationSnapshot:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        positions = [candidate.snapshot_position for candidate in candidates]
        if len(positions) != len(set(positions)) or any(position < 0 for position in positions):
            raise ValueError("snapshot positions must be unique and nonnegative")
        item_ids = {candidate.item_id for candidate in candidates}
        online_ids = set(
            session.scalars(
                select(Item.id).where(
                    Item.id.in_(item_ids), Item.online_status == OnlineStatus.ONLINE
                )
            )
        )
        if online_ids != item_ids:
            raise ValueError("snapshot contains missing or offline items")
        snapshot = RecommendationSnapshot(
            snapshot_id=snapshot_id or uuid.uuid4(),
            user_id=user_id,
            feed_type=feed_type,
            model_version=model_version,
            snapshot_seed=snapshot_seed,
            expires_at=expires_at.astimezone(UTC),
            created_at=(now or utc_now()).astimezone(UTC),
        )
        session.add(snapshot)
        session.add_all(
            [
                RecommendationSnapshotItem(
                    snapshot_id=snapshot.snapshot_id,
                    item_id=candidate.item_id,
                    source=candidate.source,
                    raw_score=candidate.raw_score,
                    normalized_score=candidate.normalized_score,
                    filter_reason=candidate.filter_reason,
                    snapshot_position=candidate.snapshot_position,
                    promotion_rule_id=candidate.promotion_rule_id,
                )
                for candidate in candidates
            ]
        )
        session.flush()
        return snapshot

    def record_page(
        self,
        session: Session,
        *,
        snapshot_id: uuid.UUID,
        user_id: uuid.UUID,
        offset: int,
        limit: int,
        latency_ms: int | None,
        page: list[PageExposure],
        request_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> RecommendationRequest:
        event_time = (now or utc_now()).astimezone(UTC)
        snapshot = session.get(RecommendationSnapshot, snapshot_id)
        if snapshot is None or snapshot.user_id != user_id:
            raise ValueError("snapshot does not belong to the authenticated user")
        if ensure_utc(snapshot.expires_at) <= event_time:
            raise ValueError("snapshot is expired")
        if offset < 0 or limit < 1 or limit > 100:
            raise ValueError("page offset/limit is outside the contract range")
        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms must be nonnegative")
        if len(page) > limit:
            raise ValueError("page length cannot exceed request limit")
        item_ids = [item.item_id for item in page]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("page item_ids must be unique")
        positions = [entry.position for entry in page]
        if positions != list(range(offset, offset + len(page))):
            raise ValueError("page positions must be contiguous from offset")
        page_ids = set(item_ids)
        online = set(
            session.scalars(
                select(Item.id).where(
                    Item.id.in_(page_ids), Item.online_status == OnlineStatus.ONLINE
                )
            )
        )
        if online != page_ids:
            raise ValueError("page contains missing or offline items")
        expected = {
            item.item_id: item
            for item in session.scalars(
                select(RecommendationSnapshotItem).where(
                    RecommendationSnapshotItem.snapshot_id == snapshot_id,
                    RecommendationSnapshotItem.item_id.in_(page_ids),
                )
            )
        }
        if len(expected) != len(page_ids):
            raise ValueError("page contains items outside the immutable snapshot")
        snapshot_positions: list[int] = []
        for entry in page:
            snapshot_item = expected[entry.item_id]
            if snapshot_item.source != entry.source:
                raise ValueError("page source must match the immutable snapshot")
            snapshot_positions.append(snapshot_item.snapshot_position)
        if any(current >= following for current, following in pairwise(snapshot_positions)):
            raise ValueError("page items must retain immutable snapshot order")
        page_request = RecommendationRequest(
            request_id=request_id or uuid.uuid4(),
            snapshot_id=snapshot_id,
            user_id=user_id,
            offset=offset,
            limit=limit,
            latency_ms=latency_ms,
            created_at=event_time,
        )
        session.add(page_request)
        session.flush()
        for entry in page:
            exposure = Exposure(
                request_id=page_request.request_id,
                snapshot_id=snapshot_id,
                user_id=user_id,
                item_id=entry.item_id,
                position=entry.position,
                source=entry.source,
                model_version=snapshot.model_version,
                exposed_at=event_time,
            )
            session.add(exposure)
            session.flush()
            impression_id = canonical_impression_event_id(
                page_request.request_id, entry.item_id, entry.position
            )
            impression_payload = {
                "canonical": True,
                "exposure_id": str(exposure.id),
            }
            session.add(
                Event(
                    event_id=impression_id,
                    exposure_id=exposure.id,
                    request_id=page_request.request_id,
                    user_id=user_id,
                    item_id=entry.item_id,
                    position=entry.position,
                    feed_type=snapshot.feed_type,
                    source=entry.source,
                    event_type=EventType.IMPRESSION,
                    client_timestamp=None,
                    server_timestamp=event_time,
                    duration_ms=None,
                    payload=impression_payload,
                    payload_hash=fingerprint(impression_payload),
                )
            )
        session.flush()
        return page_request


class EventService:
    def submit(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        request: EventRequest,
        now: datetime | None = None,
    ) -> EventItemResult:
        payload_hash = event_fingerprint(user_id, request)
        existing = session.scalar(select(Event).where(Event.event_id == request.event_id))
        if existing is not None:
            return self._classify_existing_event(existing, user_id, request, payload_hash)

        exposure_and_feed = session.execute(
            select(Exposure, RecommendationSnapshot.feed_type, Item.title)
            .join(
                RecommendationSnapshot,
                RecommendationSnapshot.snapshot_id == Exposure.snapshot_id,
            )
            .join(Item, Item.id == Exposure.item_id)
            .where(
                Exposure.request_id == request.request_id,
                Exposure.user_id == user_id,
                Exposure.item_id == request.item_id,
                Exposure.position == request.position,
            )
        ).one_or_none()
        if exposure_and_feed is None:
            return EventItemResult(
                event_id=request.event_id,
                status="rejected",
                error_code="exposure_mismatch",
                message="request, user, item, and position must match an exposure",
            )
        exposure, feed_type, item_title = exposure_and_feed
        if request.event_type == "dwell" and request.duration_ms is None:
            return EventItemResult(
                event_id=request.event_id,
                status="rejected",
                error_code="duration_required",
                message="duration_ms is required for dwell",
            )
        event_time = (now or utc_now()).astimezone(UTC)
        event = Event(
            event_id=request.event_id,
            exposure_id=exposure.id,
            request_id=request.request_id,
            user_id=user_id,
            item_id=request.item_id,
            position=request.position,
            feed_type=feed_type,
            source=exposure.source,
            event_type=EventType(request.event_type),
            client_timestamp=request.client_timestamp.astimezone(UTC),
            server_timestamp=event_time,
            duration_ms=request.duration_ms,
            payload=request.payload,
            payload_hash=payload_hash,
        )
        try:
            with session.begin_nested():
                session.add(event)
                session.flush()
        except IntegrityError:
            existing = session.scalar(select(Event).where(Event.event_id == request.event_id))
            if existing is None:
                raise
            return self._classify_existing_event(existing, user_id, request, payload_hash)
        self._update_profile(
            session,
            user_id=user_id,
            request=request,
            item_title=item_title,
            now=event_time,
        )
        session.flush()
        return EventItemResult(event_id=request.event_id, status="accepted")

    def submit_batch(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        request: EventBatchRequest,
        now: datetime | None = None,
    ) -> EventBatchResponse:
        request_hash = fingerprint({"user_id": user_id, **request.model_dump(mode="python")})
        existing = session.get(EventBatch, request.batch_id)
        if existing is not None:
            return self._replay_batch(existing, user_id, request_hash)

        started_at = (now or utc_now()).astimezone(UTC)
        batch = EventBatch(
            batch_id=request.batch_id,
            user_id=user_id,
            payload_hash=request_hash,
            item_count=len(request.events),
            started_at=started_at,
        )
        try:
            with session.begin_nested():
                session.add(batch)
                session.flush()
        except IntegrityError:
            existing = session.get(EventBatch, request.batch_id)
            if existing is None:
                raise
            return self._replay_batch(existing, user_id, request_hash)
        results: list[EventItemResult] = []
        for item in request.events:
            try:
                with session.begin_nested():
                    result = self.submit(session, user_id=user_id, request=item, now=started_at)
                    results.append(result)
            except Exception as exc:
                results.append(
                    EventItemResult(
                        event_id=item.event_id,
                        status="rejected",
                        error_code="item_transaction_failed",
                        message=type(exc).__name__,
                    )
                )
        response = EventBatchResponse(
            batch_id=request.batch_id,
            accepted=sum(result.status == "accepted" for result in results),
            duplicate=sum(result.status == "duplicate" for result in results),
            rejected=sum(result.status == "rejected" for result in results),
            results=results,
        )
        batch.accepted_count = response.accepted
        batch.duplicate_count = response.duplicate
        batch.rejected_count = response.rejected
        batch.completed_at = started_at
        batch.result = response.model_dump(mode="json")
        session.flush()
        return response

    def _update_profile(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        request: EventRequest,
        item_title: str,
        now: datetime,
    ) -> None:
        profile = session.get(UserProfile, user_id, with_for_update=True)
        if profile is None:
            profile = UserProfile(user_id=user_id)
            session.add(profile)
            session.flush()
        interaction = {
            "event_id": str(request.event_id),
            "event_type": request.event_type,
            "item_id": request.item_id,
            "server_timestamp": now.isoformat().replace("+00:00", "Z"),
        }
        profile.recent_interactions = [interaction, *list(profile.recent_interactions)][:100]
        if request.event_type == "not_interested":
            summary = dict(profile.negative_summary)
            summary[request.item_id] = int(summary.get(request.item_id, 0)) + 1
            profile.negative_summary = summary
        else:
            summary = dict(profile.positive_summary)
            summary[request.event_type] = int(summary.get(request.event_type, 0)) + 1
            profile.positive_summary = summary
        if request.event_type == "dwell":
            summary = dict(profile.dwell_summary)
            summary["event_count"] = int(summary.get("event_count", 0)) + 1
            summary["duration_ms_total"] = int(summary.get("duration_ms_total", 0)) + int(
                request.duration_ms or 0
            )
            profile.dwell_summary = summary
        if request.event_type == "revisit":
            summary = dict(profile.revisit_summary)
            summary[request.item_id] = int(summary.get(request.item_id, 0)) + 1
            profile.revisit_summary = summary
        if request.event_type == "share":
            summary = dict(profile.share_summary)
            summary[request.item_id] = int(summary.get(request.item_id, 0)) + 1
            profile.share_summary = summary
        profile.title_preferences = update_title_preferences(
            profile.title_preferences,
            title=item_title,
            event_type=request.event_type,
        )
        profile.profile_version += 1
        profile.updated_at = now

    @staticmethod
    def _classify_existing_event(
        existing: Event,
        user_id: uuid.UUID,
        request: EventRequest,
        payload_hash: str,
    ) -> EventItemResult:
        if existing.user_id == user_id and existing.payload_hash == payload_hash:
            return EventItemResult(event_id=request.event_id, status="duplicate")
        return EventItemResult(
            event_id=request.event_id,
            status="rejected",
            error_code="event_id_conflict",
            message="event_id was already used for different content",
        )

    @staticmethod
    def _replay_batch(
        existing: EventBatch, user_id: uuid.UUID, request_hash: str
    ) -> EventBatchResponse:
        if existing.user_id != user_id or existing.payload_hash != request_hash:
            from apps.api.app.auth.errors import ApiError

            raise ApiError(409, "batch_id_conflict", "batch_id has different content")
        if existing.result is None:
            raise RuntimeError("event batch exists without a completed result")
        return EventBatchResponse.model_validate(existing.result)


def title_tokens(title: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    tokens = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    return list(dict.fromkeys(token[:64] for token in tokens if token))[:TITLE_TOKEN_LIMIT]


def update_title_preferences(
    current: dict[str, Any], *, title: str, event_type: str
) -> dict[str, Any]:
    weight = TITLE_BEHAVIOR_WEIGHTS[event_type]
    preferences = {
        token: {
            "positive": int(value.get("positive", 0)),
            "negative": int(value.get("negative", 0)),
            "score": int(value.get("score", 0)),
        }
        for token, value in current.items()
        if isinstance(token, str) and isinstance(value, dict)
    }
    for token in title_tokens(title):
        entry = preferences.setdefault(token, {"positive": 0, "negative": 0, "score": 0})
        if weight > 0:
            entry["positive"] = min(100, entry["positive"] + weight)
        else:
            entry["negative"] = min(100, entry["negative"] + abs(weight))
        entry["score"] = max(-100, min(100, entry["positive"] - entry["negative"]))
    ordered = sorted(
        preferences.items(),
        key=lambda item: (-abs(int(item[1]["score"])), item[0]),
    )[:TITLE_PREFERENCE_LIMIT]
    return dict(ordered)


def profile_response(profile: UserProfile) -> UserProfileResponse:
    return UserProfileResponse(
        user_id=profile.user_id,
        profile_version=profile.profile_version,
        recent_interactions=profile.recent_interactions,
        positive_summary=profile.positive_summary,
        negative_summary=profile.negative_summary,
        dwell_summary=profile.dwell_summary,
        revisit_summary=profile.revisit_summary,
        share_summary=profile.share_summary,
        title_preferences=profile.title_preferences,
        updated_at=profile.updated_at,
    )
