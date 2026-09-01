from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from .domain import (
    AuthoritativeItem,
    BulkResult,
    IndexBuildManifest,
    ItemProjection,
    ProjectionHit,
    SearchPrincipal,
    SearchQuery,
)


class SearchProjection(Protocol):
    """Reconstructable Elasticsearch boundary; implementations wrap client errors."""

    def ping(self) -> bool: ...

    def alias_targets(self, alias: str) -> tuple[str, ...]: ...

    def index_exists(self, physical_index: str) -> bool: ...

    def create_index(
        self, physical_index: str, *, settings: dict[str, Any], mappings: dict[str, Any]
    ) -> None: ...

    def bulk_apply(
        self,
        target: str,
        *,
        upserts: tuple[ItemProjection, ...],
        deletes: tuple[str, ...],
    ) -> BulkResult: ...

    def refresh(self, target: str) -> None: ...

    def count(self, target: str) -> int: ...

    def switch_alias(
        self, alias: str, *, new_index: str, expected_old_index: str | None
    ) -> None: ...

    def search(
        self, alias: str, query: SearchQuery, *, candidate_limit: int
    ) -> list[ProjectionHit]: ...


class PostgresSearchAuthority(Protocol):
    """Every method reads current PostgreSQL state, never Elasticsearch state."""

    def iter_online_documents(self, *, batch_size: int) -> Iterable[tuple[ItemProjection, ...]]: ...

    def current_items(self, item_ids: tuple[str, ...]) -> dict[str, ItemProjection]: ...

    def authorize_hits(
        self,
        query: SearchQuery,
        principal: SearchPrincipal,
        item_ids: tuple[str, ...],
    ) -> tuple[dict[str, AuthoritativeItem], int]:
        """Return allowed online current matches and the count denied by permissions."""
        ...

    def fallback_search(
        self,
        query: SearchQuery,
        principal: SearchPrincipal,
        *,
        exclude_item_ids: tuple[str, ...],
        limit: int,
    ) -> list[AuthoritativeItem]: ...

    def fallback_ready(self) -> bool: ...


class SearchIndexRegistry(Protocol):
    """PostgreSQL-authoritative build/task metadata used for crash reconciliation."""

    def get_build(self, physical_index: str) -> IndexBuildManifest | None: ...

    def record_build(self, manifest: IndexBuildManifest) -> None: ...

    def mark_active(
        self,
        physical_index: str,
        *,
        previous_index: str | None,
        activated_at: datetime,
    ) -> None: ...

    def incremental_fingerprint(self, task_key: str) -> str | None: ...

    def record_incremental(
        self,
        task_key: str,
        *,
        fingerprint: str,
        physical_index: str,
        source_watermark: str,
        completed_at: datetime,
    ) -> None: ...

    def last_source_watermark(self) -> str | None: ...
