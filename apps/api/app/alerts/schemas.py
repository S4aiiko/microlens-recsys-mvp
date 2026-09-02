from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AlertOccurrenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurrence_id: uuid.UUID
    rule_id: uuid.UUID
    rule_name: str
    metric_name: str
    status: Literal["firing", "acknowledged", "resolved"]
    observed_value: float
    sample_count: int = Field(ge=0)
    window_start: datetime
    window_end: datetime
    fired_at: datetime
    version: int = Field(ge=1)
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    resolved_at: datetime | None
    resolve_reason: str | None


class AlertAckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate: bool
    alert: AlertOccurrenceResponse
