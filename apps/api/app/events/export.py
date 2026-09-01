from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from apps.api.app.db.models import Event, Item, TrainingExportWatermark
from recsys.data.artifacts import ParquetCodec
from recsys.data.common import (
    artifact_descriptor,
    canonical_json_bytes,
    format_utc,
    fsync_directory,
    fsync_file,
    sha256_bytes,
    sha256_file,
    validate_artifact_descriptor,
)
from recsys.data.events import validate_event_export

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
WATERMARK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$")
EXPORT_NAMESPACE = uuid.UUID("454e3cf1-ddff-42c3-9499-30fbd641cab2")
MANIFEST_KEYS = {
    "schema_version",
    "export_id",
    "event_id_ordering",
    "watermark",
    "export_cutoff_utc",
    "events_file",
    "rejected_file",
    "event_counts",
    "rejected_reason_counts",
}


class EventExportError(RuntimeError):
    """The database range could not be published without weakening its watermark."""


class ExportWatermarkCASFailure(EventExportError):
    """The artifact is published, but the expected old watermark no longer matches."""


class ExportNamespaceCollision(EventExportError):
    """A numeric export directory already belongs to another watermark namespace."""


def validate_watermark_name(name: str) -> str:
    """Require an ASCII token safe for logs, locks and future path namespaces."""

    if not isinstance(name, str) or WATERMARK_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            "watermark name must be a 1..255 character ASCII alphanumeric/underscore/hyphen token"
        )
    return name


@dataclass(frozen=True)
class ExportRange:
    name: str
    start_exclusive: int
    end_inclusive: int


@dataclass(frozen=True)
class ExportResult:
    path: Path
    manifest_checksum: str
    start_exclusive: int
    end_inclusive: int
    accepted: int
    rejected: int
    reused: bool


class TrainingExportRepository:
    """Coordinate one monotonic range inside the caller-owned transaction.

    Production takes a PostgreSQL transaction advisory lock and table snapshot lock
    first. Claim, filesystem publication and CAS remain in the same transaction: a
    process/commit failure leaves ``last_event_id`` unchanged, and the next holder
    validates and reuses (or safely rebuilds) the same artifact.
    """

    @staticmethod
    def _advisory_key(name: str) -> int:
        validate_watermark_name(name)
        raw = bytes.fromhex(sha256_bytes(name.encode("utf-8")))[:8]
        return int.from_bytes(raw, byteorder="big", signed=True)

    def lock_export_snapshot(self, session: Session, *, name: str) -> None:
        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            raise EventExportError("training event export requires PostgreSQL")
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": self._advisory_key(name)},
        )
        # Sequence ids are allocated before commit. Wait for active writers and block
        # new INSERTs while materializing the bounded range, so a lower uncommitted id
        # cannot later appear behind the advanced watermark.
        session.execute(text("LOCK TABLE events, items IN SHARE MODE"))

    def claim(self, session: Session, *, name: str) -> ExportRange:
        validate_watermark_name(name)
        watermark = session.get(TrainingExportWatermark, name, with_for_update=True)
        if watermark is None:
            watermark = TrainingExportWatermark(
                name=name, last_event_id=0, expected_checksum=None, status="idle"
            )
            session.add(watermark)
            session.flush()
        # A legacy committed exporting/failed row can be recovered safely under the
        # advisory lock from its unchanged last_event_id. This exporter never commits
        # the intermediate claim itself.
        end = session.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0
        if end < watermark.last_event_id:
            raise EventExportError("event sequence is behind the persisted watermark")
        watermark.status = "exporting"
        watermark.expected_checksum = None
        session.flush()
        return ExportRange(name, watermark.last_event_id, end)

    def events_with_metadata(
        self, session: Session, claimed: ExportRange
    ) -> list[tuple[Event, Item | None]]:
        return list(
            session.execute(
                select(Event, Item)
                .outerjoin(Item, Item.id == Event.item_id)
                .where(Event.id > claimed.start_exclusive, Event.id <= claimed.end_inclusive)
                .order_by(Event.id, Event.event_id)
            ).all()
        )

    def events(self, session: Session, claimed: ExportRange) -> list[Event]:
        """Compatibility view retained for the Phase 2B repository tests."""

        return [event for event, _item in self.events_with_metadata(session, claimed)]

    def export_cutoff(self, session: Session, claimed: ExportRange) -> datetime:
        value = session.scalar(
            select(func.max(Event.server_timestamp)).where(Event.id <= claimed.end_inclusive)
        )
        # A deterministic empty-database cutoff keeps 0-0 rebuilds byte-identical.
        return value.astimezone(UTC) if value is not None else datetime(1970, 1, 1, tzinfo=UTC)

    def complete(self, session: Session, claimed: ExportRange, *, checksum: str) -> bool:
        if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
            raise ValueError("checksum must be a lowercase SHA-256 hex digest")
        result = session.execute(
            update(TrainingExportWatermark)
            .where(
                TrainingExportWatermark.name == claimed.name,
                TrainingExportWatermark.last_event_id == claimed.start_exclusive,
                TrainingExportWatermark.status == "exporting",
            )
            .values(
                last_event_id=claimed.end_inclusive,
                expected_checksum=checksum,
                status="completed",
            )
        )
        session.flush()
        return result.rowcount == 1

    def fail(self, session: Session, claimed: ExportRange) -> None:
        session.execute(
            update(TrainingExportWatermark)
            .where(
                TrainingExportWatermark.name == claimed.name,
                TrainingExportWatermark.last_event_id == claimed.start_exclusive,
                TrainingExportWatermark.status == "exporting",
            )
            .values(status="failed", expected_checksum=None)
        )
        session.flush()


class TrainingEventExporter:
    """Write immutable accepted/rejected Parquet and then CAS the DB watermark."""

    def __init__(
        self,
        repository: TrainingExportRepository | None = None,
        codec: ParquetCodec | None = None,
    ) -> None:
        self.repository = repository or TrainingExportRepository()
        self.codec = codec or ParquetCodec()

    @staticmethod
    def _event_row(event: Event) -> dict[str, Any]:
        event_type = getattr(event.event_type, "value", event.event_type)
        return {
            "event_sequence_id": int(event.id),
            "event_id": str(event.event_id),
            "user_id": str(event.user_id),
            "request_id": str(event.request_id),
            "item_id": str(event.item_id),
            "position": int(event.position),
            "event_type": str(event_type),
            "server_timestamp": format_utc(event.server_timestamp),
            "duration_ms": None if event.duration_ms is None else int(event.duration_ms),
        }

    @staticmethod
    def _metadata_is_complete(item: Item | None) -> bool:
        return bool(
            item is not None
            and item.metadata_status == "complete"
            and item.title.strip()
            and item.likes_snapshot is not None
            and item.views_snapshot is not None
        )

    def _partition(
        self, claimed: ExportRange, rows: list[tuple[Event, Item | None]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        reasons: Counter[str] = Counter()
        for event, item in rows:
            row = self._event_row(event)
            if self._metadata_is_complete(item):
                accepted.append(row)
            else:
                reason = "missing_item_metadata"
                rejected.append({**row, "reason": reason})
                reasons[reason] += 1
        self._validate_partition(claimed, rows, accepted, rejected)
        return accepted, rejected, reasons

    @staticmethod
    def _validate_partition(
        claimed: ExportRange,
        source: list[tuple[Event, Item | None]],
        accepted: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
    ) -> None:
        source_ids = [int(event.id) for event, _item in source]
        accepted_ids = [int(row["event_sequence_id"]) for row in accepted]
        rejected_ids = [int(row["event_sequence_id"]) for row in rejected]
        if set(accepted_ids) & set(rejected_ids):
            raise EventExportError("accepted/rejected sequence overlap")
        if sorted(accepted_ids + rejected_ids) != source_ids:
            raise EventExportError("accepted/rejected sequence coverage mismatch")
        if len(set(source_ids)) != len(source_ids):
            raise EventExportError("database event sequence is not unique")
        if source_ids:
            if source_ids[0] <= claimed.start_exclusive or source_ids[-1] != claimed.end_inclusive:
                raise EventExportError("database event sequence does not cover the claimed cutoff")
        elif claimed.start_exclusive != claimed.end_inclusive:
            raise EventExportError("non-empty watermark range has no database events")

    def _rejected_schema(self):
        pa, _pq = self.codec._arrow()
        event_schema = self.codec.schemas()["events"]
        return pa.schema(
            [*event_schema, pa.field("reason", pa.string(), nullable=False)],
            metadata={b"recsys_table_contract": b"phase-2d-event-rejections-v1"},
        )

    def _write_rejected(self, path: Path, rows: list[dict[str, Any]]) -> int:
        from recsys.data.common import parse_utc

        pa, pq = self.codec._arrow()
        materialized = [
            {**row, "server_timestamp": parse_utc(str(row["server_timestamp"]))} for row in rows
        ]
        table = pa.Table.from_pylist(materialized, schema=self._rejected_schema())
        pq.write_table(
            table,
            path,
            row_group_size=self.codec.row_group_size,
            version="2.6",
            use_dictionary=False,
            compression=self.codec.compression,
            compression_level=self.codec.compression_level,
            write_statistics=True,
            data_page_size=1_048_576,
            data_page_version="1.0",
            use_compliant_nested_type=True,
            store_schema=True,
            write_page_index=False,
        )
        fsync_file(path)
        return len(materialized)

    def _read_rejected(self, path: Path) -> list[dict[str, Any]]:
        _pa, pq = self.codec._arrow()
        table = pq.read_table(path)
        if not table.schema.equals(self._rejected_schema(), check_metadata=True):
            raise EventExportError("rejected Parquet schema mismatch")
        rows = table.to_pylist()
        for row in rows:
            row["server_timestamp"] = format_utc(row["server_timestamp"])
        return rows

    @staticmethod
    def _export_id(claimed: ExportRange) -> str:
        return str(
            uuid.uuid5(
                EXPORT_NAMESPACE,
                f"{claimed.name}\0{claimed.start_exclusive}\0{claimed.end_inclusive}",
            )
        )

    @staticmethod
    def _manifest(
        claimed: ExportRange,
        cutoff: datetime,
        events_descriptor: dict[str, Any],
        rejected_descriptor: dict[str, Any],
        reasons: Counter[str],
    ) -> dict[str, Any]:
        accepted = int(events_descriptor["rows"])
        rejected = int(rejected_descriptor["rows"])
        return {
            "schema_version": "1.0",
            "export_id": TrainingEventExporter._export_id(claimed),
            "event_id_ordering": "database_sequence",
            "watermark": {
                "start_exclusive": claimed.start_exclusive,
                "end_inclusive": claimed.end_inclusive,
            },
            "export_cutoff_utc": format_utc(cutoff),
            "events_file": events_descriptor,
            "rejected_file": rejected_descriptor,
            "event_counts": {
                "accepted": accepted,
                "rejected": rejected,
                "total": accepted + rejected,
            },
            "rejected_reason_counts": dict(sorted(reasons.items())),
        }

    def _validate_with_data_contract(
        self,
        directory: Path,
        manifest: dict[str, Any],
        accepted: list[dict[str, Any]],
        known_item_ids: set[str],
    ) -> str:
        """Validate the final two-file manifest and its accepted training rows."""

        try:
            validated_manifest, validated_accepted, manifest_checksum = validate_event_export(
                directory,
                known_item_ids=known_item_ids,
                codec=self.codec,
            )
        except Exception as exc:
            raise EventExportError("event export data contract validation failed") from exc
        if validated_manifest != manifest:
            raise EventExportError("validated manifest does not match producer manifest")
        if validated_accepted != accepted:
            raise EventExportError("validated accepted rows do not match database partition")
        return manifest_checksum

    def _write_directory(
        self,
        directory: Path,
        claimed: ExportRange,
        cutoff: datetime,
        accepted: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        reasons: Counter[str],
        known_item_ids: set[str],
    ) -> tuple[dict[str, Any], str]:
        events_path = directory / "events.parquet"
        rejected_path = directory / "rejected.parquet"
        accepted_count = self.codec.write_rows(events_path, accepted)
        rejected_count = self._write_rejected(rejected_path, rejected)
        manifest = self._manifest(
            claimed,
            cutoff,
            artifact_descriptor(events_path, rows=accepted_count),
            artifact_descriptor(rejected_path, rows=rejected_count),
            reasons,
        )
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_path = directory / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        fsync_file(manifest_path)
        manifest_checksum = self._validate_with_data_contract(
            directory, manifest, accepted, known_item_ids
        )
        fsync_directory(directory)
        if manifest_checksum != sha256_bytes(manifest_bytes):
            raise EventExportError("validated manifest checksum does not match written manifest")
        return manifest, manifest_checksum

    @staticmethod
    def _validate_descriptor(directory: Path, value: Any, expected_path: str) -> dict[str, Any]:
        try:
            descriptor = validate_artifact_descriptor(value, require_rows=True)
        except ValueError as exc:
            raise EventExportError(f"invalid {expected_path} descriptor") from exc
        if descriptor["path"] != expected_path:
            raise EventExportError(f"invalid {expected_path} path")
        path = directory / expected_path
        if path.is_symlink() or not path.is_file():
            raise EventExportError(f"missing {expected_path}")
        if path.stat().st_size != descriptor["size_bytes"]:
            raise EventExportError(f"{expected_path} size mismatch")
        if sha256_file(path) != descriptor["sha256"]:
            raise EventExportError(f"{expected_path} checksum mismatch")
        return descriptor

    def _validate_existing(
        self,
        directory: Path,
        claimed: ExportRange,
        cutoff: datetime,
        accepted: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        reasons: Counter[str],
        known_item_ids: set[str],
    ) -> str:
        if directory.is_symlink() or not directory.is_dir():
            raise EventExportError("existing export is not a real directory")
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise EventExportError("existing export manifest is missing")
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = json.loads(manifest_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EventExportError("existing export manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise EventExportError("existing export manifest shape mismatch")
        if canonical_json_bytes(manifest) != manifest_bytes:
            raise EventExportError("existing export manifest is not canonical JSON")
        existing_export_id = manifest.get("export_id")
        if isinstance(existing_export_id, str) and existing_export_id != self._export_id(claimed):
            raise ExportNamespaceCollision(
                "export directory belongs to another watermark namespace"
            )
        if set(manifest) != MANIFEST_KEYS:
            raise EventExportError("existing export manifest shape mismatch")
        events_descriptor = self._validate_descriptor(
            directory, manifest["events_file"], "events.parquet"
        )
        rejected_descriptor = self._validate_descriptor(
            directory, manifest["rejected_file"], "rejected.parquet"
        )
        expected_manifest = self._manifest(
            claimed, cutoff, events_descriptor, rejected_descriptor, reasons
        )
        if manifest != expected_manifest:
            raise EventExportError("existing export manifest does not match database range")
        if int(events_descriptor["rows"]) != len(accepted):
            raise EventExportError("existing accepted row count mismatch")
        if int(rejected_descriptor["rows"]) != len(rejected):
            raise EventExportError("existing rejected row count mismatch")
        try:
            existing_accepted = self.codec.read_rows(directory / "events.parquet")
            existing_rejected = self._read_rejected(directory / "rejected.parquet")
        except Exception as exc:
            raise EventExportError("existing export Parquet validation failed") from exc
        if existing_accepted != accepted:
            raise EventExportError("existing accepted rows do not match database range")
        if existing_rejected != rejected:
            raise EventExportError("existing rejected rows do not match database range")
        manifest_checksum = self._validate_with_data_contract(
            directory, manifest, accepted, known_item_ids
        )
        if manifest_checksum != sha256_bytes(manifest_bytes):
            raise EventExportError("existing validated manifest checksum mismatch")
        return manifest_checksum

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    def _publish_directory(self, temporary: Path, final: Path) -> None:
        quarantine: Path | None = None
        if final.exists() or final.is_symlink():
            quarantine = final.parent / f".{final.name}.invalid-{uuid.uuid4().hex}"
            os.replace(final, quarantine)
            fsync_directory(final.parent)
        try:
            os.replace(temporary, final)
            fsync_directory(final.parent)
        except Exception:
            if quarantine is not None and not final.exists() and not final.is_symlink():
                os.replace(quarantine, final)
                fsync_directory(final.parent)
            raise
        if quarantine is not None:
            self._remove_path(quarantine)
            fsync_directory(final.parent)

    @staticmethod
    def _prepare_output_root(output_root: str | Path) -> Path:
        requested = Path(output_root)
        if ".." in requested.parts:
            raise EventExportError("training export root must not contain parent traversal")
        root = Path(os.path.abspath(os.fspath(requested)))

        def validate_existing_components() -> None:
            for component in reversed((root, *root.parents)):
                if component.is_symlink():
                    raise EventExportError(
                        "training export root and ancestors must not be symlinks"
                    )
                if component.exists() and not component.is_dir():
                    raise EventExportError(
                        "training export root and ancestors must be real directories"
                    )

        validate_existing_components()
        root.mkdir(parents=True, exist_ok=True)
        validate_existing_components()
        return root

    def export(
        self,
        session: Session,
        *,
        output_root: str | Path,
        watermark_name: str = "online-events",
    ) -> ExportResult:
        validate_watermark_name(watermark_name)
        self.codec.validate_runtime()
        root = self._prepare_output_root(output_root)
        fsync_directory(root)
        fsync_directory(root.parent)

        self.repository.lock_export_snapshot(session, name=watermark_name)
        claimed = self.repository.claim(session, name=watermark_name)
        source_rows = self.repository.events_with_metadata(session, claimed)
        cutoff = self.repository.export_cutoff(session, claimed)
        accepted, rejected, reasons = self._partition(claimed, source_rows)
        known_item_ids = {
            str(value)
            for value in session.scalars(
                select(Item.id).where(
                    Item.metadata_status == "complete",
                    func.length(func.trim(Item.title)) > 0,
                    Item.likes_snapshot.is_not(None),
                    Item.views_snapshot.is_not(None),
                )
            )
        }
        final = root / f"{claimed.start_exclusive}-{claimed.end_inclusive}"
        reused = False
        try:
            checksum = self._validate_existing(
                final,
                claimed,
                cutoff,
                accepted,
                rejected,
                reasons,
                known_item_ids,
            )
            reused = True
        except ExportNamespaceCollision:
            raise
        except EventExportError:
            temporary = Path(tempfile.mkdtemp(prefix=f".{final.name}.tmp-", dir=root))
            try:
                _manifest, checksum = self._write_directory(
                    temporary,
                    claimed,
                    cutoff,
                    accepted,
                    rejected,
                    reasons,
                    known_item_ids,
                )
                self._publish_directory(temporary, final)
            except Exception:
                if temporary.exists() or temporary.is_symlink():
                    self._remove_path(temporary)
                raise
        if not self.repository.complete(session, claimed, checksum=checksum):
            raise ExportWatermarkCASFailure("training export watermark CAS failed")
        return ExportResult(
            path=final,
            manifest_checksum=checksum,
            start_exclusive=claimed.start_exclusive,
            end_inclusive=claimed.end_inclusive,
            accepted=len(accepted),
            rejected=len(rejected),
            reused=reused,
        )


__all__ = [
    "EventExportError",
    "ExportNamespaceCollision",
    "ExportRange",
    "ExportResult",
    "ExportWatermarkCASFailure",
    "TrainingEventExporter",
    "TrainingExportRepository",
    "validate_watermark_name",
]
