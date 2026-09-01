from __future__ import annotations

from datetime import UTC, datetime

from apps.api.app.search.domain import (
    AuthoritativeItem,
    BulkResult,
    IndexBuildConflict,
    IndexBuildManifest,
    ItemProjection,
    ProjectionHit,
    ProjectionUnavailable,
    SearchPermissionDenied,
    SearchPrincipal,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def item(
    item_id: str,
    title: str,
    *,
    online: bool = True,
    state_version: int = 0,
    likes: int = 0,
) -> ItemProjection:
    return ItemProjection(
        item_id=item_id,
        title=title,
        likes_snapshot=likes,
        views_snapshot=likes * 10,
        online=online,
        state_version=state_version,
        updated_at=NOW,
    )


class FakeProjection:
    def __init__(self) -> None:
        self.indices: dict[str, dict[str, ItemProjection]] = {}
        self.aliases: dict[str, tuple[str, ...]] = {}
        self.reachable = True
        self.bulk_fail_ids: set[str] = set()
        self.count_delta = 0
        self.switch_failures = 0
        self.fail_after_switch = False
        self.after_bulk = None
        self.created_settings = None
        self.created_mappings = None

    def ping(self) -> bool:
        if not self.reachable:
            raise ProjectionUnavailable("offline")
        return True

    def alias_targets(self, alias: str) -> tuple[str, ...]:
        if not self.reachable:
            raise ProjectionUnavailable("offline")
        return self.aliases.get(alias, ())

    def index_exists(self, physical_index: str) -> bool:
        return physical_index in self.indices

    def create_index(self, physical_index, *, settings, mappings) -> None:
        if physical_index in self.indices:
            raise IndexBuildConflict("exists")
        self.indices[physical_index] = {}
        self.created_settings = settings
        self.created_mappings = mappings

    def bulk_apply(self, target, *, upserts, deletes) -> BulkResult:
        physical = self._target(target)
        documents = self.indices[physical]
        failures = tuple(
            sorted(
                item_id
                for item_id in [*(document.item_id for document in upserts), *deletes]
                if item_id in self.bulk_fail_ids
            )
        )
        for document in upserts:
            if document.item_id not in self.bulk_fail_ids:
                documents[document.item_id] = document
        for item_id in deletes:
            if item_id not in self.bulk_fail_ids:
                documents.pop(item_id, None)
        if self.after_bulk is not None:
            self.after_bulk()
        return BulkResult(
            succeeded=len(upserts) + len(deletes) - len(failures),
            failed_item_ids=failures,
        )

    def refresh(self, target: str) -> None:
        self._target(target)

    def count(self, target: str) -> int:
        return len(self.indices[self._target(target)]) + self.count_delta

    def switch_alias(self, alias, *, new_index, expected_old_index) -> None:
        current = self.aliases.get(alias, ())
        actual = current[0] if len(current) == 1 else None
        if actual != expected_old_index:
            raise IndexBuildConflict("alias CAS failed")
        if self.switch_failures:
            self.switch_failures -= 1
            raise ProjectionUnavailable("switch failed")
        self.aliases[alias] = (new_index,)
        if self.fail_after_switch:
            self.fail_after_switch = False
            raise ProjectionUnavailable("worker lost response after switch")

    def search(self, alias, query, *, candidate_limit):
        physical = self._target(alias)
        text = query.text.casefold()
        documents = [
            document
            for document in self.indices[physical].values()
            if text in document.item_id.casefold() or text in document.title.casefold()
        ]
        documents.sort(key=lambda document: (-(document.likes_snapshot or 0), document.item_id))
        return [
            ProjectionHit(
                item_id=document.item_id,
                score=float((document.likes_snapshot or 0) + 1),
                indexed_state_version=document.state_version,
                index_name=physical,
            )
            for document in documents[:candidate_limit]
        ]

    def _target(self, value: str) -> str:
        targets = self.aliases.get(value)
        if targets is None:
            return value
        if len(targets) != 1:
            raise ProjectionUnavailable("ambiguous alias")
        return targets[0]


class FakeAuthority:
    def __init__(self, items: list[ItemProjection]) -> None:
        self.items = {document.item_id: document for document in items}
        self.denied_item_ids: set[str] = set()
        self.denied_principals: set[object] = set()
        self.fail = False

    def iter_online_documents(self, *, batch_size):
        documents = sorted(
            (document for document in self.items.values() if document.online),
            key=lambda document: document.item_id,
        )
        for offset in range(0, len(documents), batch_size):
            yield tuple(documents[offset : offset + batch_size])

    def current_items(self, item_ids):
        if self.fail:
            raise ConnectionError("postgres unavailable")
        return {item_id: self.items[item_id] for item_id in item_ids if item_id in self.items}

    def authorize_hits(self, query, principal, item_ids):
        self._principal(principal)
        if self.fail:
            raise ConnectionError("postgres unavailable")
        allowed = {}
        denied = 0
        text = query.text.casefold()
        for item_id in item_ids:
            document = self.items.get(item_id)
            if document is None or not document.online:
                continue
            if text not in document.item_id.casefold() and text not in document.title.casefold():
                continue
            if item_id in self.denied_item_ids:
                denied += 1
                continue
            allowed[item_id] = authoritative(document)
        return allowed, denied

    def fallback_search(self, query, principal, *, exclude_item_ids, limit):
        self._principal(principal)
        if self.fail:
            raise ConnectionError("postgres unavailable")
        excluded = set(exclude_item_ids)
        text = query.text.casefold()
        matches = [
            document
            for document in self.items.values()
            if document.online
            and document.item_id not in excluded
            and document.item_id not in self.denied_item_ids
            and (text in document.item_id.casefold() or text in document.title.casefold())
        ]
        matches.sort(key=lambda document: (-(document.likes_snapshot or 0), document.item_id))
        return [authoritative(document) for document in matches[:limit]]

    def fallback_ready(self):
        return not self.fail

    def _principal(self, principal: SearchPrincipal) -> None:
        if principal.user_id in self.denied_principals:
            raise SearchPermissionDenied("denied")


class FakeRegistry:
    def __init__(self) -> None:
        self.builds: dict[str, IndexBuildManifest] = {}
        self.active = None
        self.incrementals: dict[str, str] = {}
        self.watermark = None

    def get_build(self, physical_index):
        return self.builds.get(physical_index)

    def record_build(self, manifest):
        existing = self.builds.get(manifest.physical_index)
        if existing is not None and existing != manifest:
            raise IndexBuildConflict("build conflict")
        self.builds[manifest.physical_index] = manifest

    def mark_active(self, physical_index, *, previous_index, activated_at):
        del previous_index, activated_at
        self.active = physical_index

    def incremental_fingerprint(self, task_key):
        return self.incrementals.get(task_key)

    def record_incremental(
        self,
        task_key,
        *,
        fingerprint,
        physical_index,
        source_watermark,
        completed_at,
    ):
        del physical_index, completed_at
        existing = self.incrementals.get(task_key)
        if existing is not None and existing != fingerprint:
            raise IndexBuildConflict("incremental conflict")
        self.incrementals[task_key] = fingerprint
        self.watermark = source_watermark

    def last_source_watermark(self):
        return self.watermark


def authoritative(document: ItemProjection) -> AuthoritativeItem:
    return AuthoritativeItem(
        item_id=document.item_id,
        title=document.title,
        likes_snapshot=document.likes_snapshot,
        views_snapshot=document.views_snapshot,
        state_version=document.state_version,
        updated_at=document.updated_at,
    )
