from __future__ import annotations

import json
import math
import shutil
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import ParquetCodec, TableCodec
from .common import (
    artifact_descriptor,
    canonical_json_bytes,
    format_utc,
    load_json_object,
    parse_utc,
    sha256_bytes,
    sha256_file,
    utc_to_epoch_ms,
    validate_artifact_descriptor,
    validate_relative_file_name,
)
from .errors import EventExportError, HoldoutInsufficientError, ImmutableArtifactError
from .models import BuildResult, Interaction
from .pipeline import (
    _history_rows,
    _load_immutable_manifest,
    _popularity_rows,
    _publish,
    _range,
    _write_json,
)

EVENT_TYPES = {"impression", "click", "like", "not_interested", "dwell", "revisit", "share"}
REJECTED_REASONS = {"missing_item_metadata"}


def _validate_uuid(value: Any, field: str, row_number: int) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise EventExportError(f"event row {row_number}: invalid {field}") from exc


def _read_rejected_rows(path: Path, codec: TableCodec) -> list[dict[str, Any]]:
    if isinstance(codec, ParquetCodec):
        pa, pq = codec._arrow()
        schema = pa.schema(
            [
                *codec.schemas()["events"],
                pa.field("reason", pa.string(), nullable=False),
            ],
            metadata={b"recsys_table_contract": b"phase-2d-event-rejections-v1"},
        )
        table = pq.read_table(path)
        if not table.schema.equals(schema, check_metadata=True):
            raise EventExportError("rejected Parquet schema mismatch")
        rows = table.to_pylist()
        for row in rows:
            row["server_timestamp"] = format_utc(row["server_timestamp"])
        return rows
    return codec.read_rows(path)


def validate_event_export(
    event_export: str | Path,
    *,
    known_item_ids: set[str],
    codec: TableCodec,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Validate an immutable Phase 2D export before reading it as training input."""

    export_path = Path(event_export)
    if export_path.is_symlink() or not export_path.is_dir():
        raise EventExportError("event export root must be a real directory")
    manifest_path = export_path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise EventExportError("event export manifest is missing")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EventExportError("event export manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise EventExportError("event export manifest must be an object")
    required = {
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
    missing = required - set(manifest)
    if missing:
        raise EventExportError(f"event export manifest missing fields: {sorted(missing)}")
    if manifest["schema_version"] != "1.0" or manifest["event_id_ordering"] != "database_sequence":
        raise EventExportError("unsupported event export contract")
    if not isinstance(manifest["export_id"], str) or not manifest["export_id"].strip():
        raise EventExportError("event export_id must be a non-empty string")
    try:
        file_info = validate_artifact_descriptor(manifest["events_file"], require_rows=True)
    except ValueError as exc:
        raise EventExportError(f"invalid event export file descriptor: {exc}") from exc
    relative_events_path = Path(file_info["path"])
    if file_info["path"] != f"events{codec.suffix}":
        raise EventExportError("invalid accepted event export path")
    if relative_events_path.suffix != codec.suffix:
        raise EventExportError("event export codec/extension mismatch")
    events_path = export_path / relative_events_path
    if events_path.is_symlink():
        raise EventExportError("event export data file must not be a symlink")
    if not events_path.is_file():
        raise EventExportError("event export data file is missing")
    if events_path.stat().st_size != file_info.get("size_bytes"):
        raise EventExportError("event export size/checksum mismatch")
    if sha256_file(events_path) != file_info.get("sha256"):
        raise EventExportError("event export checksum mismatch")
    rows = codec.read_rows(events_path)
    if (
        events_path.is_symlink()
        or not events_path.is_file()
        or events_path.stat().st_size != file_info["size_bytes"]
        or sha256_file(events_path) != file_info["sha256"]
    ):
        raise EventExportError("event export changed while being read")
    if len(rows) != file_info.get("rows"):
        raise EventExportError("event export row count mismatch")
    try:
        rejected_info = validate_artifact_descriptor(manifest["rejected_file"], require_rows=True)
    except ValueError as exc:
        raise EventExportError(f"invalid rejected event file descriptor: {exc}") from exc
    if rejected_info["path"] != f"rejected{codec.suffix}":
        raise EventExportError("invalid rejected event export path")
    rejected_path = export_path / rejected_info["path"]
    if rejected_path.is_symlink() or not rejected_path.is_file():
        raise EventExportError("rejected event export data file is missing")
    if (
        rejected_path.stat().st_size != rejected_info["size_bytes"]
        or sha256_file(rejected_path) != rejected_info["sha256"]
    ):
        raise EventExportError("rejected event export size/checksum mismatch")
    rejected_rows = _read_rejected_rows(rejected_path, codec)
    if (
        rejected_path.is_symlink()
        or not rejected_path.is_file()
        or rejected_path.stat().st_size != rejected_info["size_bytes"]
        or sha256_file(rejected_path) != rejected_info["sha256"]
    ):
        raise EventExportError("rejected event export changed while being read")
    if len(rejected_rows) != rejected_info["rows"]:
        raise EventExportError("rejected event export row count mismatch")
    watermark = manifest["watermark"]
    if not isinstance(watermark, dict):
        raise EventExportError("invalid export cutoff/watermark")
    try:
        cutoff = parse_utc(manifest["export_cutoff_utc"])
        start = watermark["start_exclusive"]
        end = watermark["end_inclusive"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EventExportError("invalid export cutoff/watermark") from exc
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end < start
    ):
        raise EventExportError("invalid export watermark range")
    seen_event_ids: set[str] = set()
    seen_sequences: set[int] = set()
    previous_sequence = start
    normalized: list[dict[str, Any]] = []
    for row_number, raw in enumerate(rows, start=1):
        try:
            sequence = int(raw["event_sequence_id"])
            event_id = _validate_uuid(raw["event_id"], "event_id", row_number)
            request_id = _validate_uuid(raw["request_id"], "request_id", row_number)
            user_id = str(raw["user_id"]).strip()
            item_id = str(raw["item_id"]).strip()
            event_type = str(raw["event_type"])
            position = int(raw["position"])
            server_timestamp = str(raw["server_timestamp"])
            parsed_timestamp = parse_utc(server_timestamp)
        except KeyError as exc:
            raise EventExportError(f"event row {row_number}: missing field {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise EventExportError(f"event row {row_number}: invalid field") from exc
        if sequence <= start or sequence > end:
            raise EventExportError(f"event row {row_number}: sequence beyond watermark cutoff")
        if sequence in seen_sequences or sequence <= previous_sequence:
            raise EventExportError(f"event row {row_number}: duplicate/out-of-order sequence")
        if event_id in seen_event_ids:
            raise EventExportError(f"event row {row_number}: duplicate event_id")
        if parsed_timestamp > cutoff:
            raise EventExportError(f"event row {row_number}: timestamp beyond export cutoff")
        if not user_id:
            raise EventExportError(f"event row {row_number}: empty user")
        if item_id not in known_item_ids:
            raise EventExportError(f"event row {row_number}: unknown item {item_id}")
        if event_type not in EVENT_TYPES:
            raise EventExportError(f"event row {row_number}: unknown event type")
        if position < 0:
            raise EventExportError(f"event row {row_number}: negative position")
        duration = raw.get("duration_ms")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError) as exc:
                raise EventExportError(f"event row {row_number}: invalid duration") from exc
            if duration < 0 or duration > 86_400_000:
                raise EventExportError(f"event row {row_number}: duration out of bounds")
        if event_type == "dwell" and duration is None:
            raise EventExportError(f"event row {row_number}: dwell requires duration")
        normalized.append(
            {
                "event_sequence_id": sequence,
                "event_id": event_id,
                "request_id": request_id,
                "user_id": user_id,
                "item_id": item_id,
                "position": position,
                "event_type": event_type,
                "server_timestamp": format_utc(parsed_timestamp),
                "duration_ms": duration,
            }
        )
        previous_sequence = sequence
        seen_sequences.add(sequence)
        seen_event_ids.add(event_id)
    rejected_reason_counts: Counter[str] = Counter()
    rejected_sequences: set[int] = set()
    previous_rejected_sequence = start
    for row_number, raw in enumerate(rejected_rows, start=1):
        try:
            sequence = int(raw["event_sequence_id"])
            event_id = _validate_uuid(raw["event_id"], "rejected event_id", row_number)
            _validate_uuid(raw["request_id"], "rejected request_id", row_number)
            user_id = str(raw["user_id"]).strip()
            item_id = str(raw["item_id"]).strip()
            event_type = str(raw["event_type"])
            position = int(raw["position"])
            parsed_timestamp = parse_utc(str(raw["server_timestamp"]))
            reason = str(raw["reason"])
        except KeyError as exc:
            raise EventExportError(
                f"rejected event row {row_number}: missing field {exc.args[0]}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise EventExportError(f"rejected event row {row_number}: invalid field") from exc
        if sequence <= start or sequence > end:
            raise EventExportError(
                f"rejected event row {row_number}: sequence beyond watermark cutoff"
            )
        if sequence in rejected_sequences or sequence <= previous_rejected_sequence:
            raise EventExportError(
                f"rejected event row {row_number}: duplicate/out-of-order sequence"
            )
        if sequence in seen_sequences:
            raise EventExportError("accepted/rejected event sequence overlap")
        if event_id in seen_event_ids:
            raise EventExportError("accepted/rejected duplicate event_id")
        if parsed_timestamp > cutoff:
            raise EventExportError(
                f"rejected event row {row_number}: timestamp beyond export cutoff"
            )
        if not user_id or item_id in known_item_ids:
            raise EventExportError(
                f"rejected event row {row_number}: item must be unknown/incomplete"
            )
        if event_type not in EVENT_TYPES or position < 0:
            raise EventExportError(f"rejected event row {row_number}: invalid event field")
        duration = raw.get("duration_ms")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError) as exc:
                raise EventExportError(
                    f"rejected event row {row_number}: invalid duration"
                ) from exc
            if duration < 0 or duration > 86_400_000:
                raise EventExportError(f"rejected event row {row_number}: duration out of bounds")
        if event_type == "dwell" and duration is None:
            raise EventExportError(f"rejected event row {row_number}: dwell requires duration")
        if reason not in REJECTED_REASONS:
            raise EventExportError(f"rejected event row {row_number}: unstable reason")
        rejected_reason_counts[reason] += 1
        previous_rejected_sequence = sequence
        rejected_sequences.add(sequence)
        seen_event_ids.add(event_id)

    event_counts = manifest["event_counts"]
    expected_event_counts = {
        "accepted": len(rows),
        "rejected": len(rejected_rows),
        "total": len(rows) + len(rejected_rows),
    }
    if (
        not isinstance(event_counts, dict)
        or any(
            isinstance(value, bool) or not isinstance(value, int) for value in event_counts.values()
        )
        or event_counts != expected_event_counts
    ):
        raise EventExportError("event export counts mismatch")
    reason_counts = manifest["rejected_reason_counts"]
    expected_reason_counts = dict(sorted(rejected_reason_counts.items()))
    if (
        not isinstance(reason_counts, dict)
        or any(
            not isinstance(reason, str) or isinstance(count, bool) or not isinstance(count, int)
            for reason, count in reason_counts.items()
        )
        or reason_counts != expected_reason_counts
    ):
        raise EventExportError("rejected reason counts mismatch")
    all_sequences = sorted(seen_sequences | rejected_sequences)
    if not all_sequences and start != end:
        raise EventExportError("empty event export requires an empty watermark interval")
    if all_sequences and all_sequences[-1] != end:
        raise EventExportError("accepted/rejected sequence does not cover watermark end")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise EventExportError("event export manifest changed while data was being read")
    try:
        final_manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise EventExportError("event export manifest changed while data was being read") from exc
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or final_manifest_bytes != manifest_bytes
    ):
        raise EventExportError("event export manifest changed while data was being read")
    return manifest, normalized, sha256_bytes(manifest_bytes)


def _validate_mapping(mapping: dict[str, Any]) -> None:
    if mapping.get("schema_version") != "1.0" or not mapping.get("mapping_version"):
        raise EventExportError("invalid event mapping version")
    groups: dict[str, dict[str, Any]] = {}
    for group in ("positive_weights", "negative_weights"):
        weights = mapping.get(group)
        if not isinstance(weights, dict):
            raise EventExportError(f"mapping {group} must be an object")
        groups[group] = weights
    overlap = set(groups["positive_weights"]) & set(groups["negative_weights"])
    if overlap:
        raise EventExportError(f"positive/negative mapping overlap: {sorted(overlap)}")
    if "dwell" in groups["positive_weights"] or "dwell" in groups["negative_weights"]:
        raise EventExportError("dwell must be configured only by the dwell mapping")
    allowed_by_group = {
        "positive_weights": {"click", "like", "share", "revisit"},
        "negative_weights": {"not_interested"},
    }
    for group in ("positive_weights", "negative_weights"):
        for event_type, weight in groups[group].items():
            if (
                event_type not in allowed_by_group[group]
                or isinstance(weight, bool)
                or not isinstance(weight, int | float)
                or not math.isfinite(weight)
                or weight <= 0
            ):
                raise EventExportError(f"invalid mapping weight: {group}.{event_type}")
    dwell = mapping.get("dwell", {})
    if not isinstance(dwell, dict):
        raise EventExportError("invalid dwell mapping")
    raw_minimum = dwell.get("minimum_duration_ms")
    raw_maximum = dwell.get("maximum_duration_ms")
    raw_weight = dwell.get("weight")
    if (
        isinstance(raw_minimum, bool)
        or not isinstance(raw_minimum, int)
        or isinstance(raw_maximum, bool)
        or not isinstance(raw_maximum, int)
        or isinstance(raw_weight, bool)
        or not isinstance(raw_weight, int | float)
    ):
        raise EventExportError("invalid dwell mapping")
    minimum = raw_minimum
    maximum = raw_maximum
    weight = float(raw_weight)
    if not 1 <= minimum <= maximum <= 86_400_000 or not math.isfinite(weight) or weight <= 0:
        raise EventExportError("invalid dwell mapping bounds")


def _empty_range(at: str) -> dict[str, str]:
    normalized = format_utc(parse_utc(at))
    return {"from_utc": normalized, "to_utc": normalized, "interval": "[from,to)"}


def _windows(
    purpose: str, mapping: dict[str, Any], export_cutoff: str
) -> tuple[str, dict[str, str], dict[str, str]]:
    if purpose == "systems_only":
        return export_cutoff, _empty_range(export_cutoff), _empty_range(export_cutoff)
    if purpose != "quality_evaluation":
        raise EventExportError("purpose must be systems_only or quality_evaluation")
    evaluation = mapping.get("evaluation")
    if not isinstance(evaluation, dict):
        raise EventExportError("quality_evaluation requires frozen evaluation windows")
    fields = [
        "train_end_utc",
        "validation_start_utc",
        "validation_end_utc",
        "test_start_utc",
        "test_end_utc",
    ]
    try:
        values = {field: format_utc(parse_utc(evaluation[field])) for field in fields}
    except (KeyError, TypeError, ValueError) as exc:
        raise EventExportError("invalid frozen evaluation windows") from exc
    timestamps = {field: parse_utc(value) for field, value in values.items()}
    cutoff = parse_utc(export_cutoff)
    if not (
        timestamps["train_end_utc"]
        <= timestamps["validation_start_utc"]
        < timestamps["validation_end_utc"]
        <= timestamps["test_start_utc"]
        < timestamps["test_end_utc"]
        <= cutoff
    ):
        raise EventExportError("frozen windows violate cutoff ordering")
    return (
        values["train_end_utc"],
        {
            "from_utc": values["validation_start_utc"],
            "to_utc": values["validation_end_utc"],
            "interval": "[from,to)",
        },
        {
            "from_utc": values["test_start_utc"],
            "to_utc": values["test_end_utc"],
            "interval": "[from,to)",
        },
    )


def _assign_split(
    timestamp: str, train_end: str, validation: dict[str, str], test: dict[str, str]
) -> str | None:
    value = parse_utc(timestamp)
    if value < parse_utc(train_end):
        return "train"
    if parse_utc(validation["from_utc"]) <= value < parse_utc(validation["to_utc"]):
        return "validation"
    if parse_utc(test["from_utc"]) <= value < parse_utc(test["to_utc"]):
        return "test"
    return None


def _map_events(
    rows: list[dict[str, Any]],
    mapping: dict[str, Any],
    purpose: str,
    train_end: str,
    validation: dict[str, str],
    test: dict[str, str],
    source_checksum: str,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    signals: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        split = (
            "train"
            if purpose == "systems_only"
            else _assign_split(row["server_timestamp"], train_end, validation, test)
        )
        if split is None:
            counts["rejected_outside_frozen_windows"] += 1
            continue
        event_type = row["event_type"]
        if event_type == "impression":
            label: int | None = None
            weight = 0.0
            signal_type = "exposure_context"
        elif event_type in mapping["positive_weights"]:
            label = 1
            weight = float(mapping["positive_weights"][event_type])
            signal_type = "positive"
        elif event_type in mapping["negative_weights"]:
            label = 0
            weight = float(mapping["negative_weights"][event_type])
            signal_type = "negative"
        elif event_type == "dwell":
            duration = row["duration_ms"]
            if duration < int(mapping["dwell"]["minimum_duration_ms"]):
                counts["rejected_dwell_below_threshold"] += 1
                continue
            label = 1
            minimum_duration = int(mapping["dwell"]["minimum_duration_ms"])
            maximum_duration = int(mapping["dwell"]["maximum_duration_ms"])
            effective_duration = min(duration, maximum_duration)
            weight = float(mapping["dwell"]["weight"]) * effective_duration / minimum_duration
            if duration > maximum_duration:
                counts["accepted_dwell_duration_capped"] += 1
            signal_type = "positive"
        else:
            counts["rejected_unmapped"] += 1
            continue
        signals.append(
            {
                "event_id": row["event_id"],
                "user_id": row["user_id"],
                "item_id": row["item_id"],
                "server_timestamp": row["server_timestamp"],
                "signal_type": signal_type,
                "label": label,
                "sample_weight": weight,
                "source_export_checksum": source_checksum,
                "split": split,
            }
        )
        counts[f"accepted_{split}"] += 1
    return signals, counts


def _load_base(processed_root: Path, base_data_version: str, codec: TableCodec):
    try:
        safe_version = validate_relative_file_name(base_data_version)
    except ValueError as exc:
        raise EventExportError("base_data_version must be an explicit immutable version") from exc
    if safe_version == "latest":
        raise EventExportError("base_data_version must be an explicit immutable version")
    base_path = processed_root / safe_version
    if not base_path.exists() and not base_path.is_symlink():
        raise EventExportError(f"base data version not found: {base_data_version}")
    manifest, descriptors, manifest_checksum = _load_immutable_manifest(
        base_path, expected_data_version=safe_version
    )
    if manifest.get("output_schema", {}).get("storage_format") != codec.format_name:
        raise EventExportError("base artifact codec does not match requested codec")
    required = ["items", "train", "validation", "test", "title_corpus"]
    tables = {}
    for name in required:
        filename = f"{name}{codec.suffix}"
        if filename not in descriptors:
            raise EventExportError(f"base artifact missing: {filename}")
        artifact_path = base_path / filename
        tables[name] = codec.read_rows(artifact_path)
        descriptor = descriptors[filename]
        if (
            artifact_path.is_symlink()
            or not artifact_path.is_file()
            or artifact_path.stat().st_size != descriptor["size_bytes"]
            or sha256_file(artifact_path) != descriptor["sha256"]
        ):
            raise ImmutableArtifactError(f"base artifact changed while read: {filename}")
    return base_path, manifest, manifest_checksum, tables


def build_training_data(
    base_data_version: str,
    processed_root: str | Path,
    event_export: str | Path,
    mapping_config: dict[str, Any] | str | Path,
    purpose: str,
    *,
    codec: TableCodec | None = None,
) -> BuildResult:
    """Pure, content-addressed event-feedback data build."""

    if codec is None:
        codec = ParquetCodec()
        codec.validate_runtime()
    root = Path(processed_root)
    _base_path, base_manifest, parent_checksum, base_tables = _load_base(
        root, base_data_version, codec
    )
    mapping = load_json_object(mapping_config)
    _validate_mapping(mapping)
    mapping_checksum = sha256_bytes(canonical_json_bytes(mapping))
    known_items = {str(row["item_id"]) for row in base_tables["items"]}
    export_manifest, events, export_manifest_checksum = validate_event_export(
        event_export, known_item_ids=known_items, codec=codec
    )
    train_end, validation_window, test_window = _windows(
        purpose, mapping, export_manifest["export_cutoff_utc"]
    )
    signals, mapping_counts = _map_events(
        events,
        mapping,
        purpose,
        train_end,
        validation_window,
        test_window,
        export_manifest_checksum,
    )
    if purpose == "quality_evaluation":
        evaluation = mapping["evaluation"]
        holdout_counts: dict[str, int] = {}
        try:
            minimum_interactions = int(evaluation["minimum_interactions_per_window"])
            minimum_users = int(evaluation["minimum_users_per_window"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EventExportError("invalid frozen holdout minimums") from exc
        if minimum_interactions < 1 or minimum_users < 1:
            raise EventExportError("frozen holdout minimums must be positive")
        for split in ("validation", "test"):
            split_signals = [row for row in signals if row["split"] == split and row["label"] == 1]
            holdout_counts[f"{split}_interactions"] = len(split_signals)
            holdout_counts[f"{split}_users"] = len({row["user_id"] for row in split_signals})
        if any(
            holdout_counts[f"{split}_interactions"] < minimum_interactions
            or holdout_counts[f"{split}_users"] < minimum_users
            for split in ("validation", "test")
        ):
            raise HoldoutInsufficientError(f"NOT_ENOUGH_HOLDOUT: {holdout_counts}")
    else:
        holdout_counts = {
            "validation_interactions": 0,
            "validation_users": 0,
            "test_interactions": 0,
            "test_users": 0,
        }

    base_rows = [
        Interaction(str(row["user_id"]), str(row["item_id"]), int(row["timestamp"]))
        for name in ("train", "validation", "test")
        for row in base_tables[name]
    ]
    cutoff_ms = utc_to_epoch_ms(train_end)
    # Old official validation/test can only become history when they precede the new
    # cutoff. No old split is retained as an evaluation set.
    derived_train = [row for row in base_rows if row.timestamp < cutoff_ms]
    derived_validation: list[Interaction] = []
    derived_test: list[Interaction] = []
    for signal in signals:
        if signal["label"] != 1:
            continue
        row = Interaction(
            str(signal["user_id"]),
            str(signal["item_id"]),
            utc_to_epoch_ms(str(signal["server_timestamp"])),
        )
        if signal["split"] == "train":
            derived_train.append(row)
        elif signal["split"] == "validation":
            derived_validation.append(row)
        elif signal["split"] == "test":
            derived_test.append(row)

    def sort_key(row: Interaction) -> tuple[str, int, str]:
        return (row.user_id, row.timestamp, row.item_id)

    derived_train.sort(key=sort_key)
    derived_validation.sort(key=sort_key)
    derived_test.sort(key=sort_key)

    identity = {
        "base_data_version": base_data_version,
        "parent_manifest_checksum": parent_checksum,
        "event_export_checksum": export_manifest_checksum,
        "mapping_checksum": mapping_checksum,
        "purpose": purpose,
        "train_end": train_end,
        "validation_window": validation_window,
        "test_window": test_window,
        "codec": codec.format_name,
    }
    data_version = f"{base_data_version}-events-{sha256_bytes(canonical_json_bytes(identity))[:16]}"
    temp_path = Path(tempfile.mkdtemp(prefix=f".{data_version}-", dir=root))
    try:
        artifacts: list[dict[str, Any]] = []
        items = base_tables["items"]
        memberships: dict[str, set[str]] = {item_id: set() for item_id in known_items}
        for split_name, rows in (
            ("train", derived_train),
            ("validation", derived_validation),
            ("test", derived_test),
        ):
            for row in rows:
                memberships[row.item_id].add(split_name)
        base_titles = base_tables["title_corpus"]
        title_rows = [
            {
                **row,
                "item_split_membership": sorted(memberships[str(row["item_id"])]),
                "is_train_item": "train" in memberships[str(row["item_id"])],
            }
            for row in base_titles
        ]
        time_decay = base_manifest["time_decay"]
        popularity, reference_time = _popularity_rows(
            derived_train,
            enabled=bool(time_decay["enabled"]),
            half_life_seconds=time_decay["half_life_seconds"],
        )
        histories = _history_rows(derived_train, derived_validation, derived_test)
        tables = [
            ("items", items),
            ("train", [row.as_row() for row in derived_train]),
            ("validation", [row.as_row() for row in derived_validation]),
            ("test", [row.as_row() for row in derived_test]),
            ("user_history", histories),
            ("train_popularity", popularity),
            ("title_corpus", title_rows),
            ("event_training_signals", signals),
        ]
        for name, rows in tables:
            path = temp_path / f"{name}{codec.suffix}"
            count = codec.write_rows(path, rows)
            artifacts.append(artifact_descriptor(path, rows=count))
        quality = {
            "schema_version": "1.0",
            "data_version": data_version,
            "purpose": purpose,
            "event_mapping_counts": dict(sorted(mapping_counts.items())),
            "holdout_counts": holdout_counts,
            "base_rows_reclassified_to_train": len(
                [
                    row
                    for name in ("validation", "test")
                    for row in base_tables[name]
                    if int(row["timestamp"]) < cutoff_ms
                ]
            ),
            "base_old_evaluation_reuse": "forbidden",
            "leakage_checks": {
                "train_cutoff_utc": train_end,
                "future_holdout_excluded_from_train": True,
                "negative_sampling_statistics_split": "train",
            },
        }
        quality_path = temp_path / "quality_report.json"
        _write_json(quality_path, quality)
        artifacts.insert(0, artifact_descriptor(quality_path))

        def derived_range(rows: list[Interaction], fallback: dict[str, str]) -> dict[str, str]:
            return _range(rows) if rows else fallback

        rejected = sum(
            value for key, value in mapping_counts.items() if key.startswith("rejected_")
        )
        manifest = {
            **{
                key: base_manifest[key]
                for key in (
                    "schema_version",
                    "source_urls",
                    "source_files",
                    "config_checksum",
                    "seed",
                    "input_schema",
                    "low_interaction_policy",
                    "negative_sampling_candidate_policy",
                )
            },
            "data_version": data_version,
            "output_schema": {
                **base_manifest["output_schema"],
                "event_training_signal": [
                    "event_id",
                    "user_id",
                    "item_id",
                    "server_timestamp",
                    "signal_type",
                    "label",
                    "sample_weight",
                    "source_export_checksum",
                    "split",
                ],
            },
            "record_counts": {
                "users": len(
                    {row.user_id for row in derived_train + derived_validation + derived_test}
                ),
                "items": len(items),
                "interactions": len(derived_train) + len(derived_validation) + len(derived_test),
                "train": len(derived_train),
                "validation": len(derived_validation),
                "test": len(derived_test),
            },
            "time_ranges": {
                "source": derived_range(
                    derived_train + derived_validation + derived_test,
                    _empty_range(export_manifest["export_cutoff_utc"]),
                ),
                "train": derived_range(derived_train, _empty_range(train_end)),
                "validation": derived_range(derived_validation, validation_window),
                "test": derived_range(derived_test, test_window),
            },
            "split_strategy": {
                "kind": "per_user_strict_time_holdout",
                "ordering_field": "timestamp",
                "leakage_checks": [
                    "base_old_holdout_reclassified_or_excluded",
                    "server_time_half_open_online_windows",
                    "future_holdout_excluded_from_train",
                ],
            },
            "time_decay": {**time_decay, "reference_time_utc": reference_time},
            "generation_command": canonical_json_bytes(
                {
                    "entrypoint": "recsys.data.build_training_data",
                    "parameters": {
                        "base_data_version": base_data_version,
                        "parent_manifest_checksum": parent_checksum,
                        "event_export_id": export_manifest["export_id"],
                        "event_export_manifest_checksum": export_manifest_checksum,
                        "event_file_checksum": export_manifest["events_file"]["sha256"],
                        "watermark": export_manifest["watermark"],
                        "export_cutoff_utc": export_manifest["export_cutoff_utc"],
                        "mapping_version": mapping["mapping_version"],
                        "mapping_checksum": mapping_checksum,
                        "purpose": purpose,
                        "train_cutoff_utc": train_end,
                        "validation_window_utc": validation_window,
                        "test_window_utc": test_window,
                        "codec": codec.format_name,
                    },
                }
            ).decode("utf-8"),
            "artifacts": artifacts,
            "base_data_version": base_data_version,
            "parent_manifest_checksum": parent_checksum,
            "event_export_uri": f"event-export:{export_manifest['export_id']}",
            "event_export_checksum": export_manifest_checksum,
            "event_id_watermark_range": {
                "start_exclusive": int(export_manifest["watermark"]["start_exclusive"]),
                "end_inclusive": int(export_manifest["watermark"]["end_inclusive"]),
            },
            "event_mapping_version": mapping["mapping_version"],
            "event_mapping_config_checksum": mapping_checksum,
            "event_counts": {
                "accepted": len(signals),
                "rejected": rejected,
                "deduplicated": 0,
            },
            "purpose": purpose,
            "evaluation_comparability": "comparable"
            if purpose == "quality_evaluation"
            else "non_comparable",
            "activation_eligible": purpose == "quality_evaluation",
            "train_cutoff_utc": train_end,
            "validation_window_utc": validation_window,
            "test_window_utc": test_window,
            "base_split_reuse_policy": (
                "old_validation_test_reclassify_before_train_cutoff_else_exclude"
            ),
            "holdout_counts": holdout_counts,
        }
        _write_json(temp_path / "manifest.json", manifest)
        return _publish(temp_path, root / data_version)
    except Exception:
        if temp_path.exists():
            shutil.rmtree(temp_path)
        raise


__all__ = ["build_training_data", "validate_event_export"]
