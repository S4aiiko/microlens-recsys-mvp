from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OperationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    operation_type: Literal["promote", "offline", "restore"]
    targets: list[str] = Field(min_length=1, max_length=100)
    scope_type: Literal["all", "user", "feed"] = "all"
    scope_value: str | None = None
    starts_at_utc: datetime
    ends_at_utc: datetime | None = None
    priority: int = Field(default=0, ge=0)
    target_position: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1, max_length=500)
    semantics: Literal["preflight_then_all_or_nothing_transaction"] = (
        "preflight_then_all_or_nothing_transaction"
    )

    @field_validator("starts_at_utc", "ends_at_utc")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("operation timestamps must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> OperationBatchRequest:
        if len(self.targets) != len(set(self.targets)):
            raise ValueError("targets must be unique")
        if self.ends_at_utc is not None and self.ends_at_utc <= self.starts_at_utc:
            raise ValueError("ends_at_utc must be after starts_at_utc")
        if self.scope_type == "all" and self.scope_value is not None:
            raise ValueError("all scope must not have scope_value")
        if self.scope_type != "all" and not self.scope_value:
            raise ValueError("user/feed scope requires scope_value")
        if self.operation_type != "promote" and (
            self.scope_type != "all"
            or self.scope_value is not None
            or self.target_position is not None
            or self.priority != 0
        ):
            raise ValueError("scope, priority, and target_position apply only to promotion")
        return self


class OperationBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    status: Literal["scheduled", "running", "succeeded", "failed"]
    expected_state_version: int = Field(ge=0)
    scheduled_at: datetime | None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    result: dict[str, Any] | None = None


class AuditOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: uuid.UUID
    batch_id: uuid.UUID
    operator_id: uuid.UUID
    operator_role: Literal["user", "operator_readonly", "operator", "admin"]
    operation_type: Literal["promote", "offline", "restore"]
    reason: str
    targets: list[str]
    target: str
    before_value: dict[str, Any] | None
    after_value: dict[str, Any] | None
    result: Literal["succeeded", "failed"]
    error: str | None
    effective_at: datetime


class AdminItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    heat: int = Field(ge=0)
    online_status: Literal["online", "offline"]
    updated_at: datetime
    state_version: int = Field(ge=0)
    cover: str | None


class ItemDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    cover: str | None
    position: int = Field(ge=0)
    source: str
    score: float
    reason: str
    model_version: str
