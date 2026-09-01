from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Interaction:
    user_id: str
    item_id: str
    timestamp: int

    def as_row(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "item_id": self.item_id, "timestamp": self.timestamp}


@dataclass(frozen=True, slots=True)
class Item:
    item_id: str
    title: str
    likes_snapshot: int
    views_snapshot: int

    def as_row(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "likes_snapshot": self.likes_snapshot,
            "views_snapshot": self.views_snapshot,
            "cover_ref": None,
            "metadata_status": "complete_snapshot_unusable_as_of_feature",
        }


@dataclass(frozen=True, slots=True)
class BuildResult:
    data_version: str
    path: Path
    manifest: dict[str, Any]
    manifest_checksum: str
