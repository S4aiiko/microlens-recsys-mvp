from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    item_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1)
    likes_snapshot: int | None = Field(default=None, ge=0)
    views_snapshot: int | None = Field(default=None, ge=0)
    state_version: int = Field(ge=0)
    updated_at: datetime
    retrieval_source: Literal[
        "elasticsearch_verified",
        "postgresql_backfill",
        "postgresql",
    ]
    projection_score: float | None = None


class ItemSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    items: list[SearchItemResponse]
    source: Literal[
        "elasticsearch_verified",
        "elasticsearch_with_postgresql_backfill",
        "postgresql_fallback",
    ]
    degraded: bool
    projection_index: str | None
    stale_hits_filtered: int = Field(ge=0)
    permission_hits_filtered: int = Field(ge=0)


class SearchHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["healthy", "degraded", "unavailable"]
    projection_reachable: bool
    fallback_ready: bool
    alias: str
    physical_index: str | None
    reasons: list[str]
    last_source_watermark: str | None
