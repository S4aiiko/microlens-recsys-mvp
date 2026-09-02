from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.db.models import Item, OnlineStatus, Role, User
from recsys.data.artifacts import ParquetCodec, TableCodec
from recsys.data.common import (
    SHA256_PATTERN,
    validate_artifact_descriptor,
    validate_relative_file_name,
)

from .retrieval import CatalogItem, ItemItemIndex

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TOTAL_CAPTURE_BYTES = 512 * 1024 * 1024
MAX_HISTORY_ITEMS_PER_USER = 100_000
TABLE_LIMITS = {
    "items": (1_000_000, 256 * 1024 * 1024),
    "train": (10_000_000, 256 * 1024 * 1024),
    "user_history": (1_000_000, 256 * 1024 * 1024),
}
DEMO_USERNAMES = ("demo_user_a", "demo_user_b", "demo_user_c")


def _id_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


def _immutable_histories(
    histories: Mapping[str, Sequence[str]],
) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType(
        {
            user_id: tuple(item_ids)
            for user_id, item_ids in sorted(histories.items(), key=lambda row: _id_key(row[0]))
        }
    )


@dataclass(frozen=True, slots=True)
class ServingResource:
    model_version: str
    data_version: str
    data_manifest_checksum: str
    verified_status: str
    bundle: object
    item_item_index: ItemItemIndex
    source_histories: Mapping[str, tuple[str, ...]]
    catalog_items: tuple[CatalogItem, ...]

    def __post_init__(self) -> None:
        if self.verified_status != "checksum_verified":
            raise ValueError("serving resource must be checksum verified")
        if not SHA256_PATTERN.fullmatch(self.data_manifest_checksum):
            raise ValueError("serving resource data checksum must be lowercase SHA-256")
        if self.item_item_index.source_histories != self.source_histories:
            raise ValueError("serving resource history/index identity mismatch")


@dataclass(frozen=True, slots=True)
class ServingSyncResult:
    inserted_items: int
    refreshed_items: int
    mapped_demo_users: int


class ModelStagingLoader(Protocol):
    def stage(
        self, *, artifact_uri: str, artifact_checksum: str, manifest_checksum: str
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ProcessedRecommendationLoader:
    processed_root: Path
    codec: TableCodec | None = None

    def load(
        self,
        *,
        model_version: str,
        data_version: str,
        data_manifest_checksum: str,
        bundle: object,
    ) -> ServingResource:
        try:
            safe_version = validate_relative_file_name(data_version)
        except ValueError as exc:
            raise ValueError("data_version must be one safe immutable directory name") from exc
        if safe_version.lower() == "latest":
            raise ValueError("data_version must never be latest")
        if not SHA256_PATTERN.fullmatch(data_manifest_checksum):
            raise ValueError("data_manifest_checksum must be lowercase SHA-256")
        self._validate_bundle_lineage(
            bundle,
            model_version=model_version,
            data_version=safe_version,
            data_manifest_checksum=data_manifest_checksum,
        )

        codec = self.codec or ParquetCodec()
        if isinstance(codec, ParquetCodec):
            codec.validate_runtime()
        try:
            manifest, captured, expected_rows = self._capture_version(
                safe_version,
                data_manifest_checksum=data_manifest_checksum,
                codec=codec,
            )
        except OSError as exc:
            raise ValueError("processed-data path is missing or unsafe") from exc
        if manifest.get("data_version") != safe_version:
            raise ValueError("data manifest version/path mismatch")
        output_schema = manifest.get("output_schema")
        if (
            not isinstance(output_schema, dict)
            or output_schema.get("storage_format") != codec.format_name
        ):
            raise ValueError("data storage codec does not match the manifest")

        rows = self._decode_tables(codec, captured)
        for table_name, table_rows in rows.items():
            if len(table_rows) != expected_rows[table_name]:
                raise ValueError(f"data artifact row count mismatch: {table_name}")
        catalog = self._validate_items(rows["items"])
        histories = self._validate_histories(
            train_rows=rows["train"],
            history_rows=rows["user_history"],
            item_ids={item.item_id for item in catalog},
        )
        immutable_histories = _immutable_histories(histories)
        index = ItemItemIndex.from_histories(immutable_histories)
        return ServingResource(
            model_version=model_version,
            data_version=safe_version,
            data_manifest_checksum=data_manifest_checksum,
            verified_status="checksum_verified",
            bundle=bundle,
            item_item_index=index,
            source_histories=immutable_histories,
            catalog_items=tuple(catalog),
        )

    @staticmethod
    def _validate_bundle_lineage(
        bundle: object,
        *,
        model_version: str,
        data_version: str,
        data_manifest_checksum: str,
    ) -> None:
        manifest = getattr(bundle, "manifest", None)
        if (
            getattr(bundle, "model_version", None) != model_version
            or getattr(bundle, "data_version", None) != data_version
            or not isinstance(manifest, dict)
            or manifest.get("data_manifest_checksum") != data_manifest_checksum
        ):
            raise ValueError("model bundle and processed-data lineage do not match")

    def _capture_version(
        self,
        data_version: str,
        *,
        data_manifest_checksum: str,
        codec: TableCodec,
    ) -> tuple[dict[str, Any], dict[str, bytes], dict[str, int]]:
        configured_root = Path(self.processed_root)
        if configured_root.is_symlink():
            raise ValueError("processed-data root cannot be a symlink")
        root = configured_root.resolve(strict=True)
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            version_fd = os.open(
                data_version,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            try:
                if not stat.S_ISDIR(os.fstat(version_fd).st_mode):
                    raise ValueError("data version must be a real directory")
                manifest_payload = self._capture_file(
                    version_fd, "manifest.json", max_bytes=MAX_MANIFEST_BYTES
                )
                if not hmac.compare_digest(
                    hashlib.sha256(manifest_payload).hexdigest(), data_manifest_checksum
                ):
                    raise ValueError("data manifest checksum mismatch")
                try:
                    manifest = json.loads(manifest_payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("data manifest is invalid JSON") from exc
                if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
                    raise ValueError("unsupported data manifest")
                descriptors = self._required_descriptors(manifest, codec)
                captured: dict[str, bytes] = {}
                expected_rows: dict[str, int] = {}
                total_bytes = 0
                for table_name, descriptor in descriptors.items():
                    payload = self._capture_file(
                        version_fd,
                        descriptor["path"],
                        max_bytes=TABLE_LIMITS[table_name][1],
                    )
                    total_bytes += len(payload)
                    if total_bytes > MAX_TOTAL_CAPTURE_BYTES:
                        raise ValueError("processed-data capture exceeds the total safe bound")
                    if len(payload) != descriptor["size_bytes"]:
                        raise ValueError(f"data artifact size mismatch: {descriptor['path']}")
                    if not hmac.compare_digest(
                        hashlib.sha256(payload).hexdigest(), descriptor["sha256"]
                    ):
                        raise ValueError(f"data artifact checksum mismatch: {descriptor['path']}")
                    captured[table_name] = payload
                    expected_rows[table_name] = descriptor["rows"]
            finally:
                os.close(version_fd)
        finally:
            os.close(root_fd)
        return manifest, captured, expected_rows

    @staticmethod
    def _required_descriptors(
        manifest: Mapping[str, Any], codec: TableCodec
    ) -> dict[str, dict[str, Any]]:
        raw_artifacts = manifest.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise ValueError("data manifest artifacts must be an array")
        by_path: dict[str, dict[str, Any]] = {}
        for raw in raw_artifacts:
            try:
                descriptor = validate_artifact_descriptor(raw)
            except ValueError as exc:
                raise ValueError(f"invalid data artifact descriptor: {exc}") from exc
            path = descriptor["path"]
            if path in by_path:
                raise ValueError("duplicate data artifact descriptor")
            by_path[path] = descriptor
        output: dict[str, dict[str, Any]] = {}
        for table_name, (max_rows, max_bytes) in TABLE_LIMITS.items():
            path = f"{table_name}{codec.suffix}"
            descriptor = by_path.get(path)
            if descriptor is None:
                raise ValueError(f"required data artifact is missing: {path}")
            if "rows" not in descriptor:
                raise ValueError(f"required data artifact is missing rows: {path}")
            if not 1 <= descriptor["rows"] <= max_rows:
                raise ValueError(f"data artifact row count is outside bounds: {path}")
            if not 1 <= descriptor["size_bytes"] <= max_bytes:
                raise ValueError(f"data artifact size is outside bounds: {path}")
            output[table_name] = descriptor
        return output

    @staticmethod
    def _capture_file(directory_fd: int, name: str, *, max_bytes: int) -> bytes:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"data artifact must be a regular file: {name}")
            if metadata.st_size <= 0 or metadata.st_size > max_bytes:
                raise ValueError(f"data artifact size is outside bounds: {name}")
            payload = bytearray()
            while len(payload) <= max_bytes:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
        finally:
            os.close(descriptor)
        if len(payload) > max_bytes:
            raise ValueError(f"data artifact exceeds the safe capture bound: {name}")
        return bytes(payload)

    @staticmethod
    def _decode_tables(
        codec: TableCodec, captured: Mapping[str, bytes]
    ) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        with tempfile.TemporaryDirectory(prefix="microlens-feed-data-stage-") as temporary:
            root = Path(temporary)
            for table_name, payload in captured.items():
                path = root / f"{table_name}{codec.suffix}"
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("failed to write captured data artifact")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                output[table_name] = codec.read_rows(path)
        return output

    @staticmethod
    def _validate_items(rows: Sequence[Mapping[str, Any]]) -> list[CatalogItem]:
        output: dict[str, CatalogItem] = {}
        required = {
            "item_id",
            "title",
            "likes_snapshot",
            "views_snapshot",
            "cover_ref",
            "metadata_status",
        }
        for row in rows:
            if set(row) != required:
                raise ValueError("item row fields do not match the serving contract")
            item_id = row["item_id"]
            title = row["title"]
            likes = row["likes_snapshot"]
            views = row["views_snapshot"]
            cover = row["cover_ref"]
            metadata_status = row["metadata_status"]
            if (
                not isinstance(item_id, str)
                or not 1 <= len(item_id) <= 255
                or item_id in output
                or not isinstance(title, str)
                or not 1 <= len(title) <= 10_000
                or isinstance(likes, bool)
                or not isinstance(likes, int)
                or likes < 0
                or isinstance(views, bool)
                or not isinstance(views, int)
                or views < 0
                or (cover is not None and not isinstance(cover, str))
                or not isinstance(metadata_status, str)
                or not 1 <= len(metadata_status) <= 64
            ):
                raise ValueError("item row contains invalid serving values")
            output[item_id] = CatalogItem(item_id, title, cover, likes, views, metadata_status)
        return [output[item_id] for item_id in sorted(output, key=_id_key)]

    @staticmethod
    def _validate_histories(
        *,
        train_rows: Sequence[Mapping[str, Any]],
        history_rows: Sequence[Mapping[str, Any]],
        item_ids: set[str],
    ) -> dict[str, tuple[str, ...]]:
        reconstructed: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in train_rows:
            if set(row) != {"user_id", "item_id", "timestamp"}:
                raise ValueError("train row fields do not match the serving contract")
            user_id = row["user_id"]
            item_id = row["item_id"]
            timestamp = row["timestamp"]
            if (
                not isinstance(user_id, str)
                or not 1 <= len(user_id) <= 255
                or not isinstance(item_id, str)
                or item_id not in item_ids
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
            ):
                raise ValueError("train row contains invalid serving values")
            reconstructed[user_id].append((timestamp, item_id))
        expected = {
            user_id: tuple(sorted(values, key=lambda row: (row[0], _id_key(row[1]))))
            for user_id, values in reconstructed.items()
        }
        declared: dict[str, tuple[tuple[int, str], ...]] = {}
        required = {
            "user_id",
            "ordered_item_ids",
            "ordered_timestamps",
            "split_cutoffs",
        }
        for row in history_rows:
            if set(row) != required:
                raise ValueError("user_history row fields do not match the serving contract")
            user_id = row["user_id"]
            ordered_items = row["ordered_item_ids"]
            ordered_times = row["ordered_timestamps"]
            if (
                not isinstance(user_id, str)
                or not 1 <= len(user_id) <= 255
                or user_id in declared
                or not isinstance(ordered_items, list)
                or not isinstance(ordered_times, list)
                or len(ordered_items) != len(ordered_times)
                or not 1 <= len(ordered_items) <= MAX_HISTORY_ITEMS_PER_USER
                or any(
                    not isinstance(item_id, str) or item_id not in item_ids
                    for item_id in ordered_items
                )
                or any(
                    isinstance(value, bool) or not isinstance(value, int) for value in ordered_times
                )
                or not isinstance(row["split_cutoffs"], dict)
            ):
                raise ValueError("user_history row contains invalid serving values")
            declared[user_id] = tuple(zip(ordered_times, ordered_items, strict=True))
        if expected != declared:
            raise ValueError("user_history does not exactly match train interactions")
        return {
            user_id: tuple(item_id for _timestamp, item_id in rows)
            for user_id, rows in expected.items()
        }


@dataclass(frozen=True, slots=True)
class RecommendationResourceStagingLoader:
    model_loader: ModelStagingLoader
    processed_loader: ProcessedRecommendationLoader

    def stage_activation(
        self,
        *,
        model_version: str,
        data_version: str,
        data_manifest_checksum: str,
        artifact_uri: str,
        artifact_checksum: str,
        manifest_checksum: str,
    ) -> ServingResource:
        bundle = self.model_loader.stage(
            artifact_uri=artifact_uri,
            artifact_checksum=artifact_checksum,
            manifest_checksum=manifest_checksum,
        )
        return self.processed_loader.load(
            model_version=model_version,
            data_version=data_version,
            data_manifest_checksum=data_manifest_checksum,
            bundle=bundle,
        )


def derive_feed_cursor_secret(jwt_secret: str) -> bytes:
    return hmac.new(
        jwt_secret.encode("utf-8"),
        b"microlens/feed-cursor/v1",
        hashlib.sha256,
    ).digest()


def sync_serving_resource(session: Session, resource: object) -> ServingSyncResult:
    if not isinstance(resource, ServingResource):
        raise ValueError("activation did not stage a verified serving resource")
    existing_items = {item.id: item for item in session.scalars(select(Item))}
    inserted = 0
    refreshed = 0
    for catalog_item in resource.catalog_items:
        item = existing_items.get(catalog_item.item_id)
        if item is None:
            session.add(
                Item(
                    id=catalog_item.item_id,
                    title=catalog_item.title,
                    likes_snapshot=catalog_item.likes,
                    views_snapshot=catalog_item.views,
                    cover_ref=catalog_item.cover,
                    metadata_status=catalog_item.metadata_status,
                    online_status=OnlineStatus.ONLINE,
                    state_version=0,
                )
            )
            inserted += 1
            continue
        metadata = (
            catalog_item.title,
            catalog_item.likes,
            catalog_item.views,
            catalog_item.cover,
            catalog_item.metadata_status,
        )
        current = (
            item.title,
            item.likes_snapshot,
            item.views_snapshot,
            item.cover_ref,
            item.metadata_status,
        )
        if current != metadata:
            (
                item.title,
                item.likes_snapshot,
                item.views_snapshot,
                item.cover_ref,
                item.metadata_status,
            ) = metadata
            refreshed += 1

    selected = _select_demo_source_users(resource)
    demo_users = {
        user.username_normalized: user
        for user in session.scalars(
            select(User).where(User.username_normalized.in_(DEMO_USERNAMES))
        )
    }
    if set(demo_users) != set(DEMO_USERNAMES):
        raise ValueError("all three demo users must exist before serving-resource sync")
    mapped = 0
    for username, source_user_id in zip(DEMO_USERNAMES, selected, strict=True):
        user = demo_users[username]
        if user.role != Role.USER:
            raise ValueError(f"seed identity {username} is not a user role")
        if user.source_user_id is None:
            user.source_user_id = source_user_id
            mapped += 1
        elif user.source_user_id != source_user_id:
            raise ValueError(f"seed identity {username} has a conflicting source mapping")
    session.flush()
    return ServingSyncResult(inserted, refreshed, mapped)


def _select_demo_source_users(resource: ServingResource) -> tuple[str, str, str]:
    bundle_users = set(getattr(resource.bundle, "user_ids", ()))
    candidates: list[tuple[str, tuple[str, ...]]] = []
    for user_id in sorted(resource.source_histories, key=_id_key):
        if bundle_users and user_id not in bundle_users:
            continue
        recalled = resource.item_item_index.recall(resource.source_histories[user_id], top_n=50)
        signature = tuple(row.item_id for row in recalled)
        if signature:
            candidates.append((user_id, signature))
    if len(candidates) < len(DEMO_USERNAMES):
        raise ValueError("verified histories cannot provide three demo CF mappings")
    first = candidates[0]
    second = next((candidate for candidate in candidates[1:] if candidate[1] != first[1]), None)
    if second is None:
        raise ValueError("verified histories do not provide two distinct CF signatures")
    third = next(candidate for candidate in candidates if candidate[0] not in {first[0], second[0]})
    return first[0], second[0], third[0]
