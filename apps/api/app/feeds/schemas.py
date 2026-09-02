from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class FeedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    cover: str | None
    position: int = Field(ge=0)
    source: str
    score: float
    reason: str
    model_version: str


class FeedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: uuid.UUID
    request_id: uuid.UUID
    model_version: str
    items: list[FeedItem]
    next_cursor: str | None
