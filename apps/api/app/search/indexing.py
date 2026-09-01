from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from .domain import (
    READ_ALIAS,
    BulkResult,
    FullReindexResult,
    FullReindexSpec,
    IncrementalIndexResult,
    IncrementalIndexSpec,
    IndexBuildConflict,
    IndexBuildManifest,
    ProjectionUnavailable,
    projection_document_digest,
)
from .ports import PostgresSearchAuthority, SearchIndexRegistry, SearchProjection

INDEX_SETTINGS = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "refresh_interval": "1s",
}

INDEX_MAPPINGS = {
    "dynamic": "strict",
    "properties": {
        "item_id": {"type": "keyword"},
        "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "likes_snapshot": {"type": "long"},
        "views_snapshot": {"type": "long"},
        "state_version": {"type": "long"},
        "updated_at": {"type": "date"},
    },
}


class FullReindexer:
    def __init__(
        self,
        projection: SearchProjection,
        authority: PostgresSearchAuthority,
        registry: SearchIndexRegistry,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.projection = projection
        self.authority = authority
        self.registry = registry
        self.clock = clock

    def run(self, spec: FullReindexSpec) -> FullReindexResult:
        physical_index = spec.physical_index
        current = _single_alias_target(self.projection, READ_ALIAS)
        if current == physical_index:
            return self._reconcile_activated(spec, current)
        if current != spec.expected_current_index:
            raise IndexBuildConflict(
                "alias precondition failed: "
                f"expected {spec.expected_current_index!r}, got {current!r}"
            )

        manifest = self.registry.get_build(physical_index)
        if manifest is not None:
            self._validate_manifest(spec, manifest)
            self._activate(spec, manifest, previous_index=current)
            return _full_result(manifest, previous=current, replayed=True)
        if self.projection.index_exists(physical_index):
            raise IndexBuildConflict(
                "target index exists without an authoritative completed build record; "
                "use a new version"
            )

        self.projection.create_index(
            physical_index,
            settings=INDEX_SETTINGS,
            mappings=INDEX_MAPPINGS,
        )
        digest = hashlib.sha256()
        document_count = 0
        previous_item_id: str | None = None
        for batch in self.authority.iter_online_documents(batch_size=spec.batch_size):
            if not batch:
                raise IndexBuildConflict("authority yielded an empty reindex batch")
            for document in batch:
                if not document.online:
                    raise IndexBuildConflict("full reindex authority yielded an offline item")
                if previous_item_id is not None and document.item_id <= previous_item_id:
                    raise IndexBuildConflict(
                        "full reindex source must be strictly ordered by item_id"
                    )
                previous_item_id = document.item_id
                digest.update(projection_document_digest(document))
            result = self.projection.bulk_apply(
                physical_index,
                upserts=batch,
                deletes=(),
            )
            _require_bulk_success(result, expected=len(batch))
            document_count += len(batch)

        self.projection.refresh(physical_index)
        indexed_count = self.projection.count(physical_index)
        if indexed_count != document_count:
            raise IndexBuildConflict(
                f"indexed document count mismatch: expected {document_count}, got {indexed_count}"
            )
        manifest = IndexBuildManifest(
            physical_index=physical_index,
            source_version=spec.source_version,
            build_fingerprint=spec.fingerprint,
            document_count=document_count,
            projection_checksum=digest.hexdigest(),
            built_at=_aware(self.clock()),
        )
        self.registry.record_build(manifest)
        self._activate(spec, manifest, previous_index=current)
        return _full_result(manifest, previous=current, replayed=False)

    def _activate(
        self,
        spec: FullReindexSpec,
        manifest: IndexBuildManifest,
        *,
        previous_index: str | None,
    ) -> None:
        if self.projection.count(manifest.physical_index) != manifest.document_count:
            raise IndexBuildConflict("sealed index count changed before alias activation")
        self.projection.switch_alias(
            READ_ALIAS,
            new_index=manifest.physical_index,
            expected_old_index=previous_index,
        )
        active = _single_alias_target(self.projection, READ_ALIAS)
        if active != manifest.physical_index:
            raise IndexBuildConflict("alias switch did not produce the requested single target")
        self.registry.mark_active(
            manifest.physical_index,
            previous_index=previous_index,
            activated_at=_aware(self.clock()),
        )

    def _reconcile_activated(self, spec: FullReindexSpec, current: str) -> FullReindexResult:
        manifest = self.registry.get_build(current)
        if manifest is None:
            raise IndexBuildConflict("active alias target has no authoritative build record")
        self._validate_manifest(spec, manifest)
        if self.projection.count(current) != manifest.document_count:
            raise IndexBuildConflict(
                "active index count differs from its authoritative build record"
            )
        self.registry.mark_active(
            current,
            previous_index=spec.expected_current_index,
            activated_at=_aware(self.clock()),
        )
        return _full_result(manifest, previous=spec.expected_current_index, replayed=True)

    @staticmethod
    def _validate_manifest(spec: FullReindexSpec, manifest: IndexBuildManifest) -> None:
        if manifest.physical_index != spec.physical_index:
            raise IndexBuildConflict("build record names a different physical index")
        if manifest.source_version != spec.source_version:
            raise IndexBuildConflict("build record source version differs from task input")
        if manifest.build_fingerprint != spec.fingerprint:
            raise IndexBuildConflict("build record fingerprint differs from task input")


class IncrementalIndexer:
    def __init__(
        self,
        projection: SearchProjection,
        authority: PostgresSearchAuthority,
        registry: SearchIndexRegistry,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.projection = projection
        self.authority = authority
        self.registry = registry
        self.clock = clock

    def run(self, spec: IncrementalIndexSpec) -> IncrementalIndexResult:
        existing = self.registry.incremental_fingerprint(spec.task_key)
        if existing is not None:
            if existing != spec.fingerprint:
                raise IndexBuildConflict("incremental task key was reused with different input")
            current = _single_alias_target(self.projection, READ_ALIAS)
            if current is None:
                raise ProjectionUnavailable("search alias has no target")
            return IncrementalIndexResult(
                physical_index=current,
                upserted=0,
                deleted=0,
                source_watermark=spec.source_watermark,
                replayed=True,
            )

        before = _single_alias_target(self.projection, READ_ALIAS)
        if before is None:
            raise ProjectionUnavailable("search alias has no target")
        current_items = self.authority.current_items(spec.item_ids)
        upserts = tuple(
            current_items[item_id]
            for item_id in sorted(current_items)
            if current_items[item_id].online
        )
        deletes = tuple(
            item_id
            for item_id in sorted(spec.item_ids)
            if item_id not in current_items or not current_items[item_id].online
        )
        result = self.projection.bulk_apply(READ_ALIAS, upserts=upserts, deletes=deletes)
        _require_bulk_success(result, expected=len(upserts) + len(deletes))
        after = _single_alias_target(self.projection, READ_ALIAS)
        if after != before:
            raise IndexBuildConflict(
                "alias changed during incremental indexing; retry against the current target"
            )
        if spec.refresh:
            self.projection.refresh(READ_ALIAS)
        self.registry.record_incremental(
            spec.task_key,
            fingerprint=spec.fingerprint,
            physical_index=after,
            source_watermark=spec.source_watermark,
            completed_at=_aware(self.clock()),
        )
        return IncrementalIndexResult(
            physical_index=after,
            upserted=len(upserts),
            deleted=len(deletes),
            source_watermark=spec.source_watermark,
            replayed=False,
        )


def _single_alias_target(projection: SearchProjection, alias: str) -> str | None:
    targets = projection.alias_targets(alias)
    if len(targets) > 1:
        raise IndexBuildConflict("read alias must resolve to at most one physical index")
    return targets[0] if targets else None


def _require_bulk_success(result: BulkResult, *, expected: int) -> None:
    if not result.ok or result.succeeded != expected:
        failed = ",".join(result.failed_item_ids[:10])
        raise ProjectionUnavailable(
            f"bulk indexing did not fully succeed: expected={expected} "
            f"succeeded={result.succeeded} failed={failed}"
        )


def _full_result(
    manifest: IndexBuildManifest, *, previous: str | None, replayed: bool
) -> FullReindexResult:
    return FullReindexResult(
        physical_index=manifest.physical_index,
        previous_index=previous,
        document_count=manifest.document_count,
        projection_checksum=manifest.projection_checksum,
        replayed=replayed,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
