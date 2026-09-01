from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ClientEventType = Literal["click", "like", "not_interested", "dwell", "revisit", "share"]
EventStatus = Literal["accepted", "duplicate", "rejected"]


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    request_id: uuid.UUID
    item_id: str = Field(min_length=1)
    position: int = Field(ge=0)
    event_type: ClientEventType
    client_timestamp: datetime
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("client_timestamp")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("client_timestamp must include an offset")
        return value

    @model_validator(mode="after")
    def dwell_requires_duration(self) -> EventRequest:
        if self.event_type == "dwell" and self.duration_ms is None:
            raise ValueError("duration_ms is required for dwell")
        return self


class EventItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    status: EventStatus
    error_code: str | None = None
    message: str | None = None


class EventBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    events: list[EventRequest] = Field(min_length=1, max_length=100)


class EventBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    semantics: Literal["per_item_atomic_partial_success"] = "per_item_atomic_partial_success"
    accepted: int = Field(ge=0)
    duplicate: int = Field(ge=0)
    rejected: int = Field(ge=0)
    results: list[EventItemResult]


class UserProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    profile_version: int = Field(ge=0)
    recent_interactions: list[dict[str, Any]]
    positive_summary: dict[str, Any]
    negative_summary: dict[str, Any]
    dwell_summary: dict[str, Any]
    revisit_summary: dict[str, Any]
    share_summary: dict[str, Any]
    title_preferences: dict[str, Any]
    updated_at: datetime
