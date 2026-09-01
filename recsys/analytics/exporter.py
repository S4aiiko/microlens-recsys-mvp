from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    EVENT_TYPES,
    FEED_TYPES,
    AnalyticsContractError,
    AnalyticsSnapshot,
    EventRow,
    ExposureRow,
    require_utc,
)
from .schema import EVENTS_CONTRACT, EXPOSURES_CONTRACT, SCHEMA_VERSION, pyarrow_schemas

EXPORT_ID_RE = re.compile(r"^analytics-[0-9a-f]{20}$")
SAFE_PARTITION_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExportResult:
    export_id: str
    path: Path
    manifest_checksum: str
    reused: bool


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_utc(value: datetime) -> str:
    return require_utc(value, "timestamp").isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AnalyticsContractError(f"{field} must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AnalyticsContractError(f"{field} must be an ISO-8601 UTC string") from exc
    return parsed.astimezone(UTC)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_real_directory(path: Path) -> Path:
    if path.is_symlink():
        raise AnalyticsContractError(f"directory must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise AnalyticsContractError(f"path must be a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise AnalyticsContractError("resolved output root must be a directory")
    return resolved


def _validate_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AnalyticsContractError(f"invalid {field}") from exc


def _require_real_path_below(root: Path, path: Path) -> None:
    current = path
    while current != root:
        if current.is_symlink():
            raise AnalyticsContractError("analytics path or ancestor must not be a symlink")
        if root not in current.parents:
            raise AnalyticsContractError("analytics path escaped export root")
        current = current.parent


def _validate_count_map(actual: dict[str, int], allowed: tuple[str, ...], field: str) -> None:
    if set(actual) != set(allowed):
        raise AnalyticsContractError(f"{field} must contain exactly {list(allowed)}")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in actual.values()
    ):
        raise AnalyticsContractError(f"{field} values must be non-negative integers")


def _validate_snapshot(snapshot: AnalyticsSnapshot) -> None:
    previous = snapshot.previous_event_sequence_exclusive
    cutoff = snapshot.event_sequence_cutoff_inclusive
    if (
        isinstance(previous, bool)
        or not isinstance(previous, int)
        or isinstance(cutoff, bool)
        or not isinstance(cutoff, int)
        or previous < 0
        or cutoff < previous
    ):
        raise AnalyticsContractError("invalid monotonic event watermark")
    _validate_count_map(snapshot.postgres_event_counts, EVENT_TYPES, "postgres_event_counts")
    _validate_count_map(snapshot.postgres_exposure_counts, FEED_TYPES, "postgres_exposure_counts")
    seen_event_ids: set[str] = set()
    seen_sequences: set[int] = set()
    last_sequence = previous
    event_counts: Counter[str] = Counter()
    exposure_ids: set[str] = set()
    for row in snapshot.exposures:
        exposure_id = _validate_uuid(row.exposure_id, "exposure_id")
        _validate_uuid(row.request_id, "request_id")
        _validate_uuid(row.snapshot_id, "snapshot_id")
        _validate_uuid(row.user_id, "user_id")
        timestamp = require_utc(row.exposed_at, "exposed_at")
        if not snapshot.window.from_utc <= timestamp < snapshot.window.to_utc:
            raise AnalyticsContractError("exposure lies outside UTC [from,to) window")
        if row.feed_type not in FEED_TYPES or not SAFE_PARTITION_RE.fullmatch(row.feed_type):
            raise AnalyticsContractError("unknown/unsafe exposure feed_type")
        if not row.item_id or not row.source or not row.model_version or row.position < 0:
            raise AnalyticsContractError("invalid exposure business fields")
        if not previous < row.canonical_event_sequence_id <= cutoff:
            raise AnalyticsContractError("exposure canonical sequence lies outside watermark")
        if exposure_id in exposure_ids:
            raise AnalyticsContractError("duplicate exposure_id")
        exposure_ids.add(exposure_id)
    exposure_counts = Counter(row.feed_type for row in snapshot.exposures)
    for row in snapshot.events:
        event_id = _validate_uuid(row.event_id, "event_id")
        exposure_id = _validate_uuid(row.exposure_id, "event exposure_id")
        _validate_uuid(row.request_id, "event request_id")
        _validate_uuid(row.user_id, "event user_id")
        server_timestamp = require_utc(row.server_timestamp, "server_timestamp")
        if not snapshot.window.from_utc <= server_timestamp < snapshot.window.to_utc:
            raise AnalyticsContractError("event lies outside UTC [from,to) window")
        if row.client_timestamp is not None:
            require_utc(row.client_timestamp, "client_timestamp")
        if row.event_type not in EVENT_TYPES or not SAFE_PARTITION_RE.fullmatch(row.event_type):
            raise AnalyticsContractError("unknown/unsafe event_type")
        if row.feed_type not in FEED_TYPES:
            raise AnalyticsContractError("unknown event feed_type")
        if row.position < 0 or not row.item_id or not row.source:
            raise AnalyticsContractError("invalid event business fields")
        if row.duration_ms is not None and not 0 <= row.duration_ms <= 86_400_000:
            raise AnalyticsContractError("event duration is outside contract")
        if row.event_type == "dwell" and row.duration_ms is None:
            raise AnalyticsContractError("dwell event requires duration_ms")
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError as exc:
            raise AnalyticsContractError("payload_json must be valid JSON") from exc
        if (
            not isinstance(payload, dict)
            or _canonical_bytes(payload).decode().strip() != row.payload_json
        ):
            raise AnalyticsContractError("payload_json must be a canonical JSON object")
        if not previous < row.event_sequence_id <= cutoff:
            raise AnalyticsContractError("event sequence lies outside watermark")
        if row.event_sequence_id <= last_sequence or row.event_sequence_id in seen_sequences:
            raise AnalyticsContractError("event rows must be strictly sequence ordered")
        if event_id in seen_event_ids:
            raise AnalyticsContractError("duplicate event_id")
        last_sequence = row.event_sequence_id
        seen_sequences.add(row.event_sequence_id)
        seen_event_ids.add(event_id)
        event_counts[row.event_type] += 1
    if {kind: event_counts[kind] for kind in EVENT_TYPES} != snapshot.postgres_event_counts:
        raise AnalyticsContractError("event rows do not reconcile with PostgreSQL aggregates")
    if {kind: exposure_counts[kind] for kind in FEED_TYPES} != snapshot.postgres_exposure_counts:
        raise AnalyticsContractError("exposure rows do not reconcile with PostgreSQL aggregates")


def _identity(
    snapshot: AnalyticsSnapshot, parent_manifest_checksum: str | None
) -> dict[str, object]:
    if parent_manifest_checksum is not None and not SHA256_RE.fullmatch(parent_manifest_checksum):
        raise AnalyticsContractError("parent_manifest_checksum must be SHA-256 or null")
    if snapshot.previous_event_sequence_exclusive == 0 and parent_manifest_checksum is not None:
        raise AnalyticsContractError("initial watermark export cannot name a parent manifest")
    if snapshot.previous_event_sequence_exclusive > 0 and parent_manifest_checksum is None:
        raise AnalyticsContractError("advanced watermark export requires a parent manifest")
    return {
        "schema_version": SCHEMA_VERSION,
        "window": {
            "from_utc": _format_utc(snapshot.window.from_utc),
            "to_utc": _format_utc(snapshot.window.to_utc),
            "semantics": "half_open_[from,to)",
        },
        "source_watermark": {
            "ordering": "postgres_events.id",
            "previous_event_sequence_exclusive": snapshot.previous_event_sequence_exclusive,
            "event_sequence_cutoff_inclusive": snapshot.event_sequence_cutoff_inclusive,
        },
        "parent_manifest_checksum": parent_manifest_checksum,
        "late_event_policy": {
            "policy": "immutable_follow_up_revision",
            "description": (
                "Rows committed after the captured events.id cutoff are excluded from this "
                "immutable "
                "export and require a new export with this manifest checksum as parent."
            ),
        },
    }


class AnalyticsExporter:
    def __init__(self, *, allow_unsupported_pyarrow: bool = False) -> None:
        self.allow_unsupported_pyarrow = allow_unsupported_pyarrow

    def publish(
        self,
        snapshot: AnalyticsSnapshot,
        output_root: str | Path,
        *,
        parent_manifest_checksum: str | None = None,
    ) -> ExportResult:
        _validate_snapshot(snapshot)
        identity = _identity(snapshot, parent_manifest_checksum)
        export_id = f"analytics-{_sha256_bytes(_canonical_bytes(identity))[:20]}"
        root = _ensure_real_directory(Path(output_root))
        target = root / export_id
        if target.exists():
            manifest, checksum = validate_export(
                target, allow_unsupported_pyarrow=self.allow_unsupported_pyarrow
            )
            if manifest["identity"] != identity:
                raise AnalyticsContractError("existing immutable export identity mismatch")
            return ExportResult(export_id, target, checksum, True)
        stage = Path(tempfile.mkdtemp(prefix=f".{export_id}-", dir=root))
        try:
            files = self._write_partitions(stage, snapshot)
            manifest_without_checksum: dict[str, object] = {
                "export_id": export_id,
                "identity": identity,
                "format": "hive-compatible-partitioned-parquet",
                "hive_runtime_validation": "NOT_RUN_NO_REAL_HIVE",
                "writer": {
                    "implementation": "pyarrow",
                    "required_version": "25.0.1",
                    "deterministic_settings": {
                        "compression": "zstd",
                        "compression_level": 3,
                        "row_group_size": 65_536,
                        "parquet_version": "2.6",
                        "use_dictionary": False,
                    },
                },
                "schemas": {
                    "events": EVENTS_CONTRACT.canonical_dict()
                    | {"fingerprint": EVENTS_CONTRACT.fingerprint},
                    "exposures": EXPOSURES_CONTRACT.canonical_dict()
                    | {"fingerprint": EXPOSURES_CONTRACT.fingerprint},
                },
                "postgres_counts": {
                    "events": {kind: snapshot.postgres_event_counts[kind] for kind in EVENT_TYPES},
                    "exposures": {
                        kind: snapshot.postgres_exposure_counts[kind] for kind in FEED_TYPES
                    },
                },
                "files": files,
            }
            payload_checksum = _sha256_bytes(_canonical_bytes(manifest_without_checksum))
            manifest = manifest_without_checksum | {"manifest_payload_checksum": payload_checksum}
            manifest_bytes = _canonical_bytes(manifest)
            manifest_checksum = _sha256_bytes(manifest_bytes)
            manifest_path = stage / "manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            _fsync_file(manifest_path)
            checksum_path = stage / "manifest.sha256"
            checksum_path.write_text(f"{manifest_checksum}  manifest.json\n", encoding="ascii")
            _fsync_file(checksum_path)
            _fsync_directory(stage)
            os.replace(stage, target)
            _fsync_directory(root)
            validate_export(target, allow_unsupported_pyarrow=self.allow_unsupported_pyarrow)
            return ExportResult(export_id, target, manifest_checksum, False)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    def _write_partitions(
        self, stage: Path, snapshot: AnalyticsSnapshot
    ) -> list[dict[str, object]]:
        pa, schemas = pyarrow_schemas(allow_unsupported_version=self.allow_unsupported_pyarrow)
        import pyarrow.parquet as pq

        grouped_events: dict[tuple[str, str], list[EventRow]] = defaultdict(list)
        grouped_exposures: dict[tuple[str, str], list[ExposureRow]] = defaultdict(list)
        for row in snapshot.events:
            grouped_events[(row.server_timestamp.date().isoformat(), row.event_type)].append(row)
        for row in snapshot.exposures:
            grouped_exposures[(row.exposed_at.date().isoformat(), row.feed_type)].append(row)
        descriptors: list[dict[str, object]] = []
        for (day, event_type), rows in sorted(grouped_events.items()):
            relative = (
                Path("events") / f"dt={day}" / f"event_type={event_type}" / "part-00000.parquet"
            )
            records = []
            for row in sorted(rows, key=lambda value: (value.event_sequence_id, value.event_id)):
                record = asdict(row)
                record.pop("event_type")
                record["client_timestamp"] = (
                    require_utc(row.client_timestamp, "client_timestamp")
                    if row.client_timestamp is not None
                    else None
                )
                record["server_timestamp"] = require_utc(row.server_timestamp, "server_timestamp")
                record["schema_version"] = SCHEMA_VERSION
                records.append(record)
            descriptors.append(
                self._write_file(pa, pq, stage, relative, schemas["events"], records, "events")
            )
        for (day, feed_type), rows in sorted(grouped_exposures.items()):
            relative = (
                Path("exposures") / f"dt={day}" / f"feed_type={feed_type}" / "part-00000.parquet"
            )
            records = []
            for row in sorted(
                rows, key=lambda value: (value.canonical_event_sequence_id, value.exposure_id)
            ):
                record = asdict(row)
                record.pop("feed_type")
                record["exposed_at"] = require_utc(row.exposed_at, "exposed_at")
                record["schema_version"] = SCHEMA_VERSION
                records.append(record)
            descriptors.append(
                self._write_file(
                    pa, pq, stage, relative, schemas["exposures"], records, "exposures"
                )
            )
        return sorted(descriptors, key=lambda descriptor: str(descriptor["path"]))

    @staticmethod
    def _write_file(pa, pq, stage: Path, relative: Path, schema, rows, dataset: str):
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=schema)
        pq.write_table(
            table,
            destination,
            row_group_size=65_536,
            version="2.6",
            use_dictionary=False,
            compression="zstd",
            compression_level=3,
            write_statistics=True,
            data_page_size=1_048_576,
            data_page_version="1.0",
            store_schema=True,
            write_page_index=False,
        )
        _fsync_file(destination)
        return {
            "dataset": dataset,
            "path": relative.as_posix(),
            "rows": len(rows),
            "size_bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
        }


def validate_export(
    export_path: str | Path, *, allow_unsupported_pyarrow: bool = False
) -> tuple[dict[str, Any], str]:
    root = Path(export_path)
    if root.is_symlink() or not root.is_dir() or not EXPORT_ID_RE.fullmatch(root.name):
        raise AnalyticsContractError("analytics export root is missing, symlinked or misnamed")
    manifest_path = root / "manifest.json"
    checksum_path = root / "manifest.sha256"
    if (
        manifest_path.is_symlink()
        or checksum_path.is_symlink()
        or not manifest_path.is_file()
        or not checksum_path.is_file()
    ):
        raise AnalyticsContractError("analytics manifest/checksum is missing or symlinked")
    manifest_bytes = manifest_path.read_bytes()
    manifest_checksum = _sha256_bytes(manifest_bytes)
    if checksum_path.read_text(encoding="ascii") != f"{manifest_checksum}  manifest.json\n":
        raise AnalyticsContractError("analytics manifest sidecar checksum mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AnalyticsContractError("analytics manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("export_id") != root.name:
        raise AnalyticsContractError("analytics manifest export identity mismatch")
    if manifest_bytes != _canonical_bytes(manifest):
        raise AnalyticsContractError("analytics manifest must use canonical JSON bytes")
    payload_checksum = manifest.get("manifest_payload_checksum")
    if not isinstance(payload_checksum, str) or not SHA256_RE.fullmatch(payload_checksum):
        raise AnalyticsContractError("analytics manifest payload checksum is invalid")
    body = dict(manifest)
    body.pop("manifest_payload_checksum")
    if _sha256_bytes(_canonical_bytes(body)) != payload_checksum:
        raise AnalyticsContractError("analytics manifest payload checksum mismatch")
    if manifest.get("format") != "hive-compatible-partitioned-parquet":
        raise AnalyticsContractError("unsupported analytics format")
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or identity.get("schema_version") != SCHEMA_VERSION:
        raise AnalyticsContractError("unsupported analytics identity/schema")
    if f"analytics-{_sha256_bytes(_canonical_bytes(identity))[:20]}" != root.name:
        raise AnalyticsContractError("analytics export id does not match identity")
    window = identity.get("window")
    watermark = identity.get("source_watermark")
    late_policy = identity.get("late_event_policy")
    parent_checksum = identity.get("parent_manifest_checksum")
    if not isinstance(window, dict) or set(window) != {"from_utc", "to_utc", "semantics"}:
        raise AnalyticsContractError("invalid analytics UTC window")
    from_utc = _parse_utc(window["from_utc"], "from_utc")
    to_utc = _parse_utc(window["to_utc"], "to_utc")
    if from_utc >= to_utc or window["semantics"] != "half_open_[from,to)":
        raise AnalyticsContractError("invalid analytics half-open window")
    if not isinstance(watermark, dict) or set(watermark) != {
        "ordering",
        "previous_event_sequence_exclusive",
        "event_sequence_cutoff_inclusive",
    }:
        raise AnalyticsContractError("invalid analytics source watermark")
    previous = watermark["previous_event_sequence_exclusive"]
    cutoff = watermark["event_sequence_cutoff_inclusive"]
    if (
        watermark["ordering"] != "postgres_events.id"
        or isinstance(previous, bool)
        or not isinstance(previous, int)
        or isinstance(cutoff, bool)
        or not isinstance(cutoff, int)
        or previous < 0
        or cutoff < previous
    ):
        raise AnalyticsContractError("invalid analytics monotonic source watermark")
    if parent_checksum is not None and (
        not isinstance(parent_checksum, str) or not SHA256_RE.fullmatch(parent_checksum)
    ):
        raise AnalyticsContractError("invalid analytics parent manifest checksum")
    if (
        not isinstance(late_policy, dict)
        or late_policy.get("policy") != "immutable_follow_up_revision"
        or set(late_policy) != {"policy", "description"}
        or not isinstance(late_policy.get("description"), str)
    ):
        raise AnalyticsContractError("invalid analytics late-event policy")
    schemas = manifest.get("schemas")
    if not isinstance(schemas, dict):
        raise AnalyticsContractError("analytics schemas are missing")
    for name, contract in (("events", EVENTS_CONTRACT), ("exposures", EXPOSURES_CONTRACT)):
        declared = schemas.get(name)
        expected = contract.canonical_dict() | {"fingerprint": contract.fingerprint}
        if declared != expected:
            raise AnalyticsContractError(f"analytics {name} schema contract mismatch")
    pa, physical_schemas = pyarrow_schemas(allow_unsupported_version=allow_unsupported_pyarrow)
    import pyarrow.parquet as pq

    files = manifest.get("files")
    if not isinstance(files, list):
        raise AnalyticsContractError("analytics files must be a list")
    declared_paths: set[str] = set()
    counted: dict[str, Counter[str]] = {
        "events": Counter(),
        "exposures": Counter(),
    }
    seen_event_ids: set[str] = set()
    seen_event_sequences: set[int] = set()
    seen_exposure_ids: set[str] = set()
    for descriptor in files:
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "dataset",
            "path",
            "rows",
            "size_bytes",
            "sha256",
        }:
            raise AnalyticsContractError("invalid analytics file descriptor")
        dataset = descriptor["dataset"]
        path_text = descriptor["path"]
        rows = descriptor["rows"]
        size = descriptor["size_bytes"]
        checksum = descriptor["sha256"]
        if dataset not in {"events", "exposures"} or not isinstance(path_text, str):
            raise AnalyticsContractError("invalid analytics dataset/path")
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".parquet":
            raise AnalyticsContractError("unsafe analytics relative path")
        if path_text in declared_paths:
            raise AnalyticsContractError("duplicate analytics file path")
        parts = relative.parts
        expected_key = "event_type" if dataset == "events" else "feed_type"
        if (
            len(parts) != 4
            or parts[0] != dataset
            or not re.fullmatch(r"dt=\d{4}-\d{2}-\d{2}", parts[1])
            or not parts[2].startswith(f"{expected_key}=")
            or parts[3] != "part-00000.parquet"
        ):
            raise AnalyticsContractError("analytics path is not frozen Hive-style layout")
        partition_value = parts[2].split("=", maxsplit=1)[1]
        allowed = EVENT_TYPES if dataset == "events" else FEED_TYPES
        if partition_value not in allowed:
            raise AnalyticsContractError("analytics partition value is not allowlisted")
        if (
            isinstance(rows, bool)
            or not isinstance(rows, int)
            or rows <= 0
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(checksum, str)
            or not SHA256_RE.fullmatch(checksum)
        ):
            raise AnalyticsContractError("invalid analytics rows/size/checksum")
        path = root / relative
        _require_real_path_below(root, path)
        if not path.is_file():
            raise AnalyticsContractError("analytics data file is missing or symlinked")
        if path.stat().st_size != size or _sha256_file(path) != checksum:
            raise AnalyticsContractError("analytics data file size/checksum mismatch")
        # Read the physical file directly. ``pq.read_table`` discovers Hive
        # partition columns from ancestor paths and would change this schema.
        table = pq.ParquetFile(path).read()
        if not table.schema.equals(physical_schemas[dataset], check_metadata=True):
            raise AnalyticsContractError("analytics physical Parquet schema mismatch")
        if table.num_rows != rows:
            raise AnalyticsContractError("analytics Parquet row count mismatch")
        partition_day = parts[1].split("=", maxsplit=1)[1]
        for row in table.to_pylist():
            if row.get("schema_version") != SCHEMA_VERSION:
                raise AnalyticsContractError("analytics row schema version mismatch")
            if dataset == "events":
                sequence = row["event_sequence_id"]
                event_id = _validate_uuid(row["event_id"], "Parquet event_id")
                if not previous < sequence <= cutoff or sequence in seen_event_sequences:
                    raise AnalyticsContractError("Parquet event sequence violates watermark")
                if event_id in seen_event_ids:
                    raise AnalyticsContractError("duplicate Parquet event_id")
                timestamp = require_utc(row["server_timestamp"], "Parquet server_timestamp")
                if (
                    not from_utc <= timestamp < to_utc
                    or timestamp.date().isoformat() != partition_day
                ):
                    raise AnalyticsContractError("Parquet event violates UTC window/partition")
                try:
                    payload = json.loads(row["payload_json"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise AnalyticsContractError("Parquet event payload is invalid") from exc
                if not isinstance(payload, dict):
                    raise AnalyticsContractError("Parquet event payload must be an object")
                seen_event_sequences.add(sequence)
                seen_event_ids.add(event_id)
            else:
                sequence = row["canonical_event_sequence_id"]
                exposure_id = _validate_uuid(row["exposure_id"], "Parquet exposure_id")
                if not previous < sequence <= cutoff:
                    raise AnalyticsContractError("Parquet exposure sequence violates watermark")
                if exposure_id in seen_exposure_ids:
                    raise AnalyticsContractError("duplicate Parquet exposure_id")
                timestamp = require_utc(row["exposed_at"], "Parquet exposed_at")
                if (
                    not from_utc <= timestamp < to_utc
                    or timestamp.date().isoformat() != partition_day
                ):
                    raise AnalyticsContractError("Parquet exposure violates UTC window/partition")
                seen_exposure_ids.add(exposure_id)
        if path.is_symlink() or path.stat().st_size != size or _sha256_file(path) != checksum:
            raise AnalyticsContractError("analytics data file changed while reading")
        counted[dataset][partition_value] += rows
        declared_paths.add(path_text)
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual_paths != declared_paths | {"manifest.json", "manifest.sha256"}:
        raise AnalyticsContractError("analytics export contains missing/unlisted files")
    postgres_counts = manifest.get("postgres_counts")
    if not isinstance(postgres_counts, dict):
        raise AnalyticsContractError("analytics PostgreSQL counts are missing")
    expected_events = {kind: counted["events"][kind] for kind in EVENT_TYPES}
    expected_exposures = {kind: counted["exposures"][kind] for kind in FEED_TYPES}
    if postgres_counts.get("events") != expected_events:
        raise AnalyticsContractError("event Parquet counts do not match PostgreSQL counts")
    if postgres_counts.get("exposures") != expected_exposures:
        raise AnalyticsContractError("exposure Parquet counts do not match PostgreSQL counts")
    del pa
    return manifest, manifest_checksum


def validate_export_chain(
    previous_export: str | Path,
    next_export: str | Path,
    *,
    allow_unsupported_pyarrow: bool = False,
) -> None:
    previous_manifest, previous_checksum = validate_export(
        previous_export, allow_unsupported_pyarrow=allow_unsupported_pyarrow
    )
    next_manifest, _ = validate_export(
        next_export, allow_unsupported_pyarrow=allow_unsupported_pyarrow
    )
    previous_identity = previous_manifest["identity"]
    next_identity = next_manifest["identity"]
    if next_identity["parent_manifest_checksum"] != previous_checksum:
        raise AnalyticsContractError("analytics export chain parent checksum mismatch")
    if next_identity["window"] != previous_identity["window"]:
        raise AnalyticsContractError("analytics export chain window changed")
    previous_watermark = previous_identity["source_watermark"]
    next_watermark = next_identity["source_watermark"]
    if (
        next_watermark["previous_event_sequence_exclusive"]
        != previous_watermark["event_sequence_cutoff_inclusive"]
    ):
        raise AnalyticsContractError("analytics export chain watermark is not contiguous")
