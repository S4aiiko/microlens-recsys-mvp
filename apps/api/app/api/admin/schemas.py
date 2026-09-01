from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.events.schemas import UserProfileResponse

FeedName = Literal["personalized", "popular", "explore"]


class DashboardOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_utc: datetime
    to_utc: datetime
    timezone: Literal["UTC"] = "UTC"
    total_users: int = Field(ge=0)
    active_users: int = Field(ge=0)
    requests: int = Field(ge=0)
    exposures: int = Field(ge=0)
    clicks: int = Field(ge=0)
    likes: int = Field(ge=0)
    shares: int = Field(ge=0)
    revisits: int = Field(ge=0)
    dwell_ms_total: int = Field(ge=0)
    offline_item_count: int = Field(ge=0)
    ctr: float = Field(ge=0)
    zero_denominator: bool
    active_model_version: str | None


class DashboardBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_start_utc: datetime
    bucket_end_utc: datetime
    feed_type: FeedName
    request_count: int = Field(ge=0)
    exposure_count: int = Field(ge=0)
    click_count: int = Field(ge=0)
    like_count: int = Field(ge=0)
    share_count: int = Field(ge=0)
    revisit_count: int = Field(ge=0)
    dwell_ms_total: int = Field(ge=0)
    dwell_ms_avg: float = Field(ge=0)
    ctr: float = Field(ge=0)
    active_user_count: int = Field(ge=0)


class DashboardFeedDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_utc: datetime
    to_utc: datetime
    feeds: list[DashboardBucket]
    feed_share: dict[FeedName, float]


class FeedItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    cover: str | None
    position: int = Field(ge=0)
    source: str
    score: float
    reason: str
    model_version: str


class PersistedEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    request_id: uuid.UUID
    item_id: str
    position: int = Field(ge=0)
    event_type: Literal[
        "impression", "click", "like", "not_interested", "dwell", "revisit", "share"
    ]
    client_timestamp: datetime | None
    server_timestamp: datetime
    duration_ms: int | None
    payload: dict[str, Any]


class UserDebugResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    profile: UserProfileResponse
    recent_request_ids: list[uuid.UUID]


class RecommendationRequestDebugResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    candidate_item_ids: list[str]
    filtered_item_ids: list[str]
    ranked_items: list[FeedItemResponse]
    events: list[PersistedEventResponse]


class HotItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    exposure_count: int = Field(ge=0)
    click_count: int = Field(ge=0)
    like_count: int = Field(ge=0)
