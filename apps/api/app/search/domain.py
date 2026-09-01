from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

INDEX_PREFIX = "microlens-items"
READ_ALIAS = f"{INDEX_PREFIX}-read"
_VERSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SearchError(RuntimeError):
    pass


class ProjectionUnavailable(SearchError):
    """The reconstructable Elasticsearch projection cannot serve the operation."""


class AuthorityUnavailable(SearchError):
    """PostgreSQL authority could not verify the requested result."""


class SearchPermissionDenied(PermissionError):
    """The current PostgreSQL principal is missing, disabled, or unauthorized."""


class IndexBuildConflict(SearchError):
    """A versioned index is dirty, concurrently changed, or otherwise unsafe."""


class IndexHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SearchPrincipal:
    user_id: UUID


@dataclass(frozen=True)
class SearchQuery:
    text: str
    limit: int = 20

    def __post_init__(self) -> None:
        normalized = " ".join(self.text.split())
        if not normalized or len(normalized) > 200:
            raise ValueError("search text must contain 1..200 normalized characters")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("search limit must be an integer")
        if self.limit < 1 or self.limit > 100:
            raise ValueError("search limit must be between 1 and 100")
        object.__setattr__(self, "text", normalized)


@dataclass(frozen=True)
class ItemProjection:
    item_id: str
    title: str
    likes_snapshot: int | None
    views_snapshot: int | None
    online: bool
    state_version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.item_id or len(self.item_id) > 255:
            raise ValueError("item_id must contain 1..255 characters")
        if not self.title:
            raise ValueError("title must not be empty")
        if not isinstance(self.online, bool):
            raise ValueError("online must be boolean")
        if isinstance(self.state_version, bool) or not isinstance(self.state_version, int):
            raise ValueError("state_version must be an integer")
        if self.state_version < 0:
            raise ValueError("state_version must be non-negative")
        _require_aware(self.updated_at, field="updated_at")
        for field_name in ("likes_snapshot", "views_snapshot"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} must be non-negative when present")

    def as_document(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "likes_snapshot": self.likes_snapshot,
            "views_snapshot": self.views_snapshot,
            "state_version": self.state_version,
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True)
class ProjectionHit:
    item_id: str
    score: float
    indexed_state_version: int
    index_name: str

    def __post_init__(self) -> None:
        if not self.item_id or len(self.item_id) > 255:
            raise ValueError("projection hit item_id must contain 1..255 characters")
        if not math.isfinite(self.score):
            raise ValueError("projection hit score must be finite")
        if isinstance(self.indexed_state_version, bool) or not isinstance(
            self.indexed_state_version, int
        ):
            raise ValueError("indexed_state_version must be an integer")
        if self.indexed_state_version < 0:
            raise ValueError("indexed_state_version must be non-negative")
        validate_physical_index(self.index_name)


@dataclass(frozen=True)
class AuthoritativeItem:
    item_id: str
    title: str
    likes_snapshot: int | None
    views_snapshot: int | None
    state_version: int
    updated_at: datetime


@dataclass(frozen=True)
class SearchResultItem:
    item: AuthoritativeItem
    retrieval_source: str
    projection_score: float | None


@dataclass(frozen=True)
class SearchResponse:
    items: tuple[SearchResultItem, ...]
    source: str
    degraded: bool
    projection_index: str | None
    stale_hits_filtered: int
    permission_hits_filtered: int


@dataclass(frozen=True)
class BulkResult:
    succeeded: int
    failed_item_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.succeeded, bool) or not isinstance(self.succeeded, int):
            raise ValueError("bulk succeeded count must be an integer")
        if self.succeeded < 0:
            raise ValueError("bulk succeeded count must be non-negative")
        if len(set(self.failed_item_ids)) != len(self.failed_item_ids):
            raise ValueError("bulk failed item ids must be unique")

    @property
    def ok(self) -> bool:
        return not self.failed_item_ids


@dataclass(frozen=True)
class FullReindexSpec:
    index_version: str
    source_version: str
    expected_current_index: str | None = None
    batch_size: int = 500

    def __post_init__(self) -> None:
        validate_version(self.index_version)
        if (
            not self.source_version
            or len(self.source_version) > 255
            or self.source_version == "latest"
        ):
            raise ValueError("source_version must be an explicit immutable version")
        if self.expected_current_index is not None:
            validate_physical_index(self.expected_current_index)
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int):
            raise ValueError("batch_size must be an integer")
        if self.batch_size < 1 or self.batch_size > 5000:
            raise ValueError("batch_size must be between 1 and 5000")
        if self.physical_index == self.expected_current_index:
            raise ValueError("new physical index must differ from current index")

    @property
    def physical_index(self) -> str:
        return physical_index_name(self.index_version)

    @property
    def fingerprint(self) -> str:
        return stable_digest(
            {
                "index_version": self.index_version,
                "source_version": self.source_version,
                "expected_current_index": self.expected_current_index,
                "batch_size": self.batch_size,
            }
        )


@dataclass(frozen=True)
class IndexBuildManifest:
    physical_index: str
    source_version: str
    build_fingerprint: str
    document_count: int
    projection_checksum: str
    built_at: datetime

    def __post_init__(self) -> None:
        validate_physical_index(self.physical_index)
        if not self.source_version or len(self.source_version) > 255:
            raise ValueError("manifest source_version must contain 1..255 characters")
        for field_name in ("build_fingerprint", "projection_checksum"):
            value = getattr(self, field_name)
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        if isinstance(self.document_count, bool) or not isinstance(self.document_count, int):
            raise ValueError("document_count must be an integer")
        if self.document_count < 0:
            raise ValueError("document_count must be non-negative")
        _require_aware(self.built_at, field="built_at")


@dataclass(frozen=True)
class FullReindexResult:
    physical_index: str
    previous_index: str | None
    document_count: int
    projection_checksum: str
    replayed: bool


@dataclass(frozen=True)
class IncrementalIndexSpec:
    task_key: str
    item_ids: tuple[str, ...]
    source_watermark: str
    refresh: bool = False

    def __post_init__(self) -> None:
        if not self.task_key or len(self.task_key) > 255:
            raise ValueError("task_key must contain 1..255 characters")
        if not self.source_watermark or len(self.source_watermark) > 255:
            raise ValueError("source_watermark must contain 1..255 characters")
        if not self.item_ids or len(self.item_ids) > 1000:
            raise ValueError("incremental task must contain 1..1000 item ids")
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("incremental item ids must be unique")
        if any(not item_id or len(item_id) > 255 for item_id in self.item_ids):
            raise ValueError("incremental item ids must contain 1..255 characters")

    @property
    def fingerprint(self) -> str:
        return stable_digest(
            {
                "task_key": self.task_key,
                "item_ids": sorted(self.item_ids),
                "source_watermark": self.source_watermark,
                "refresh": self.refresh,
            }
        )


@dataclass(frozen=True)
class IncrementalIndexResult:
    physical_index: str
    upserted: int
    deleted: int
    source_watermark: str
    replayed: bool


@dataclass(frozen=True)
class SearchHealthReport:
    status: IndexHealth
    projection_reachable: bool
    fallback_ready: bool
    alias: str
    physical_index: str | None
    reasons: tuple[str, ...]
    last_source_watermark: str | None


def physical_index_name(version: str) -> str:
    validate_version(version)
    return f"{INDEX_PREFIX}-{version}"


def validate_version(version: str) -> None:
    if not _VERSION_PATTERN.fullmatch(version) or version in {".", "..", "read"}:
        raise ValueError("index_version must be a safe lowercase immutable token")


def validate_physical_index(name: str) -> None:
    prefix = f"{INDEX_PREFIX}-"
    if not name.startswith(prefix):
        raise ValueError("physical index is outside the microlens items namespace")
    if name == READ_ALIAS:
        raise ValueError("read alias cannot be used as a physical index")
    validate_version(name.removeprefix(prefix))


def stable_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def projection_document_digest(document: ItemProjection) -> bytes:
    payload = json.dumps(
        document.as_document(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{payload}\n".encode()


def _require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
