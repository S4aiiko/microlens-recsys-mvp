from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: str = Field(min_length=1, max_length=255)
    task_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    payload: dict[str, Any]
    due_at: datetime
    max_attempts: StrictInt = Field(default=3, ge=1, le=100)

    @field_validator("due_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("due_at must include a timezone offset")
        return value


class JobRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("due_at must include a timezone offset")
        return value


class DurableJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    idempotency_key: str
    task_name: str
    state: str
    due_at: datetime
    max_attempts: int = Field(ge=1, le=100)
    attempt_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    result: dict[str, Any] | None
    last_error: str | None


class JobMutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: bool | None = None
    duplicate: bool | None = None
    job: DurableJobResponse
