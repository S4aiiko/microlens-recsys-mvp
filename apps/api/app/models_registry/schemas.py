from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Purpose = Literal["base_official", "systems_only", "quality_evaluation"]
Comparability = Literal["non_comparable", "comparable"]


class ModelVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version: str
    data_version: str
    status: Literal["TRAINING", "EVALUATED", "READY", "ACTIVE", "ARCHIVED", "FAILED"]
    purpose: Purpose
    evaluation_comparability: Comparability
    activation_eligible: bool
    metrics: dict[str, float]
    trained_at: datetime | None
    published_at: datetime | None


class ModelComparisonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versions: list[ModelVersionResponse] = Field(min_length=2)
    comparable: bool
    reason: str | None


class TrainingJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    idempotency_key: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    data_version: str
    data_manifest_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    purpose: Purpose
    evaluation_comparability: Comparability
    activation_eligible: bool
    failure_reason: str | None


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_version: str | None
    manifest_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
