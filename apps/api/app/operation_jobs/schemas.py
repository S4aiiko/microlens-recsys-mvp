from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from apps.api.app.async_runtime.schemas import DurableJobResponse


class OperationTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target_id: str = Field(min_length=1, max_length=255)
    state_version: StrictInt = Field(ge=0)


class OperationJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=220)
    kind: Literal["promote", "offline", "restore"]
    targets: list[OperationTargetRequest] = Field(min_length=1, max_length=100)
    due_at: datetime
    ends_at_utc: datetime | None = None
    scope_type: Literal["all", "user", "feed"] = "all"
    scope_value: str | None = Field(default=None, max_length=255)
    priority: StrictInt = Field(default=0, ge=0)
    target_position: StrictInt | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1, max_length=500)
    max_attempts: StrictInt = Field(default=3, ge=1, le=100)

    @field_validator("due_at", "ends_at_utc")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("operation timestamps must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> OperationJobCreateRequest:
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("operation targets must be unique")
        if self.ends_at_utc is not None and self.ends_at_utc <= self.due_at:
            raise ValueError("ends_at_utc must be after due_at")
        if self.scope_type == "all" and self.scope_value is not None:
            raise ValueError("all scope must not have scope_value")
        if self.scope_type != "all" and not self.scope_value:
            raise ValueError("user/feed scope requires scope_value")
        if self.kind != "promote" and (
            self.scope_type != "all"
            or self.scope_value is not None
            or self.priority != 0
            or self.target_position is not None
            or self.ends_at_utc is not None
        ):
            raise ValueError("promotion scheduling fields apply only to promote jobs")
        return self


class OperationJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: bool | None = None
    duplicate: bool | None = None
    job: DurableJobResponse
