from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .artifacts import ParquetCodec, TableCodec
from .common import (
    artifact_descriptor,
    canonical_json_bytes,
    epoch_ms_to_utc,
    fsync_directory,
    fsync_file,
    load_json_object,
    sha256_bytes,
    sha256_file,
    validate_artifact_descriptor,
)
from .errors import DataQualityError, ImmutableArtifactError
from .models import BuildResult, Interaction, Item
from .parsing import inspect_official_files, load_official


def _id_key(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def normalize_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", title).split())


def _stable_smoke_users(interactions: Iterable[Interaction], count: int, seed: int) -> set[str]:
    users = {row.user_id for row in interactions}
    ranked = sorted(
        users,
        key=lambda user_id: (
            hashlib.sha256(f"{seed}\0{user_id}".encode()).digest(),
            _id_key(user_id),
        ),
    )
    return set(ranked[:count])


def split_interactions(
    interactions: Iterable[Interaction], *, min_train_interactions: int
) -> tuple[list[Interaction], list[Interaction], list[Interaction], dict[str, Any]]:
    """Split whole timestamp groups so train < validation < test for every eval user."""

    by_user: dict[str, list[Interaction]] = defaultdict(list)
    for row in interactions:
        by_user[row.user_id].append(row)
    train: list[Interaction] = []
    validation: list[Interaction] = []
    test: list[Interaction] = []
    train_only_users = 0
    eval_users = 0
    for user_id in sorted(by_user, key=_id_key):
        rows = sorted(by_user[user_id], key=lambda row: (row.timestamp, _id_key(row.item_id)))
        timestamps = sorted({row.timestamp for row in rows})
        if len(timestamps) < 3:
            train.extend(rows)
            train_only_users += 1
            continue
        validation_timestamp, test_timestamp = timestamps[-2:]
        user_train = [row for row in rows if row.timestamp < validation_timestamp]
        if len(user_train) < min_train_interactions:
            train.extend(rows)
            train_only_users += 1
            continue
        user_validation = [row for row in rows if row.timestamp == validation_timestamp]
        user_test = [row for row in rows if row.timestamp == test_timestamp]
        if not max(row.timestamp for row in user_train) < validation_timestamp < test_timestamp:
            raise DataQualityError(f"strict temporal split leakage for user {user_id}")
        train.extend(user_train)
        validation.extend(user_validation)
        test.extend(user_test)
        eval_users += 1

    def key(row: Interaction) -> tuple[tuple[int, int | str], int, tuple[int, int | str]]:
        return (_id_key(row.user_id), row.timestamp, _id_key(row.item_id))

    train.sort(key=key)
    validation.sort(key=key)
    test.sort(key=key)
    return (
        train,
        validation,
        test,
        {
            "train_only_users": train_only_users,
            "evaluation_users": eval_users,
            "tie_policy": "keep_equal_timestamp_group_in_one_split",
        },
    )


def _range(rows: list[Interaction]) -> dict[str, str]:
    if not rows:
        raise DataQualityError("required split is empty")
    start = min(row.timestamp for row in rows)
    end_exclusive = max(row.timestamp for row in rows) + 1
    return {
        "from_utc": epoch_ms_to_utc(start),
        "to_utc": epoch_ms_to_utc(end_exclusive),
        "interval": "[from,to)",
    }


def _history_rows(
    train: list[Interaction], validation: list[Interaction], test: list[Interaction]
) -> list[dict[str, Any]]:
    by_user: dict[str, list[Interaction]] = defaultdict(list)
    for row in train:
        by_user[row.user_id].append(row)
    validation_cutoff = {row.user_id: row.timestamp for row in validation}
    test_cutoff = {row.user_id: row.timestamp for row in test}
    output = []
    for user_id in sorted(by_user, key=_id_key):
        rows = sorted(by_user[user_id], key=lambda row: (row.timestamp, _id_key(row.item_id)))
        output.append(
            {
                "user_id": user_id,
                "ordered_item_ids": [row.item_id for row in rows],
                "ordered_timestamps": [row.timestamp for row in rows],
                "split_cutoffs": {
                    "validation_timestamp": validation_cutoff.get(user_id),
                    "test_timestamp": test_cutoff.get(user_id),
                },
            }
        )
    return output


def _popularity_rows(
    train: list[Interaction], *, enabled: bool, half_life_seconds: int | None
) -> tuple[list[dict[str, Any]], str | None]:
    counts = Counter(row.item_id for row in train)
    total = sum(counts.values())
    reference_ms = max(row.timestamp for row in train)
    weighted: dict[str, float] = defaultdict(float)
    for row in train:
        if enabled:
            if not half_life_seconds or half_life_seconds < 1:
                raise DataQualityError("enabled time decay requires positive half_life_seconds")
            age_seconds = max(0.0, (reference_ms - row.timestamp) / 1000)
            weighted[row.item_id] += math.exp(-math.log(2) * age_seconds / half_life_seconds)
        else:
            weighted[row.item_id] += 1.0
    rows = [
        {
            "item_id": item_id,
            "count": counts[item_id],
            "probability": counts[item_id] / total,
            "time_decayed_count": weighted[item_id],
        }
        for item_id in sorted(counts, key=_id_key)
    ]
    return rows, epoch_ms_to_utc(reference_ms) if enabled else None


def _title_rows(
    items: list[Item],
    train: list[Interaction],
    validation: list[Interaction],
    test: list[Interaction],
) -> list[dict[str, Any]]:
    memberships: dict[str, set[str]] = defaultdict(set)
    for split, rows in (("train", train), ("validation", validation), ("test", test)):
        for row in rows:
            memberships[row.item_id].add(split)
    return [
        {
            "item_id": item.item_id,
            "normalized_title": normalize_title(item.title),
            "item_split_membership": sorted(memberships[item.item_id]),
            "is_train_item": "train" in memberships[item.item_id],
        }
        for item in items
    ]


def _write_json(path: Path, value: Any) -> None:
    with path.open("wb") as handle:
        handle.write(canonical_json_bytes(value))
        handle.write(b"\n")
    fsync_file(path)


def _validate_config(config: dict[str, Any]) -> None:
    if config.get("schema_version") != "1.0":
        raise DataQualityError("config schema_version must be 1.0")
    if not isinstance(config.get("seed"), int) or config["seed"] < 0:
        raise DataQualityError("config seed must be a non-negative integer")
    source_urls = config.get("source_urls")
    if (
        not isinstance(source_urls, list)
        or not source_urls
        or not all(isinstance(value, str) and value.startswith("https://") for value in source_urls)
    ):
        raise DataQualityError("config source_urls must contain HTTPS official sources")
    split = config.get("split", {})
    if split.get("low_interaction", "train_only") != "train_only":
        raise DataQualityError("only the train_only low-interaction policy is supported")
    if int(split.get("min_train_interactions", 1)) < 1:
        raise DataQualityError("min_train_interactions must be positive")
    sampling = config.get("negative_sampling", {})
    if set(sampling.get("strategies", [])) != {"uniform", "popularity_aware"}:
        raise DataQualityError("both frozen negative-sampling strategies are required")
    if float(sampling.get("popularity_alpha", 0.75)) < 0:
        raise DataQualityError("popularity_alpha must be non-negative")
    time_decay = config.get("time_decay", {})
    if time_decay.get("enabled") and int(time_decay.get("half_life_seconds", 0)) < 1:
        raise DataQualityError("enabled time decay requires a positive half life")


def _load_immutable_manifest(
    path: Path,
    *,
    expected_data_version: str | None = None,
    require_directory_name: bool = True,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    import json

    if path.is_symlink() or not path.is_dir():
        raise ImmutableArtifactError(f"immutable data directory must be a real directory: {path}")
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ImmutableArtifactError(f"existing immutable directory lacks manifest: {path}")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ImmutableArtifactError(
            f"immutable manifest is invalid JSON: {manifest_path}"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
        raise ImmutableArtifactError("immutable manifest must be a schema 1.0 object")
    data_version = manifest.get("data_version")
    if not isinstance(data_version, str):
        raise ImmutableArtifactError("immutable manifest data_version must be a string")
    if require_directory_name and data_version != path.name:
        raise ImmutableArtifactError("immutable manifest data_version must match directory name")
    if expected_data_version is not None and data_version != expected_data_version:
        raise ImmutableArtifactError("immutable manifest data_version mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ImmutableArtifactError("immutable manifest artifacts must be a non-empty array")
    descriptors: dict[str, dict[str, Any]] = {}
    for raw_descriptor in artifacts:
        try:
            descriptor = validate_artifact_descriptor(raw_descriptor)
        except ValueError as exc:
            raise ImmutableArtifactError(f"invalid immutable artifact descriptor: {exc}") from exc
        artifact_name = descriptor["path"]
        if artifact_name == "manifest.json":
            raise ImmutableArtifactError("manifest.json cannot describe itself as an artifact")
        if artifact_name in descriptors:
            raise ImmutableArtifactError(f"duplicate immutable artifact path: {artifact_name}")
        artifact_path = path / artifact_name
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ImmutableArtifactError(f"immutable artifact must be a real file: {artifact_path}")
        if artifact_path.stat().st_size != descriptor["size_bytes"]:
            raise ImmutableArtifactError(f"immutable artifact size mismatch: {artifact_path}")
        if sha256_file(artifact_path) != descriptor["sha256"]:
            raise ImmutableArtifactError(f"immutable artifact checksum mismatch: {artifact_path}")
        descriptors[artifact_name] = descriptor
    return manifest, descriptors, sha256_bytes(manifest_bytes)


def _verify_existing(path: Path) -> BuildResult:
    manifest, _, manifest_checksum = _load_immutable_manifest(path, expected_data_version=path.name)
    return BuildResult(manifest["data_version"], path, manifest, manifest_checksum)


def _publish(temp_path: Path, final_path: Path) -> BuildResult:
    _, _, candidate_manifest_checksum = _load_immutable_manifest(
        temp_path,
        expected_data_version=final_path.name,
        require_directory_name=False,
    )
    fsync_directory(temp_path)
    if final_path.exists() or final_path.is_symlink():
        existing = _verify_existing(final_path)
        if existing.manifest_checksum != candidate_manifest_checksum:
            raise ImmutableArtifactError(f"data version collision: {final_path.name}")
        shutil.rmtree(temp_path)
        return existing
    temp_path.replace(final_path)
    fsync_directory(final_path.parent)
    return _verify_existing(final_path)


def build_official_dataset(
    config: dict[str, Any] | str | Path,
    raw_dir: str | Path,
    output_root: str | Path,
    *,
    codec: TableCodec | None = None,
) -> BuildResult:
    resolved = load_json_object(config)
    _validate_config(resolved)
    if codec is None:
        codec = ParquetCodec()
        codec.validate_runtime()
    raw_path = Path(raw_dir)
    output_path = Path(output_root)
    quality_config = resolved.get("quality", {})
    interactions, items, raw_quality = load_official(
        raw_path,
        duplicate_policy=quality_config.get("duplicate_policy", "reject"),
        orphan_policy=quality_config.get("orphan_policy", "reject"),
    )
    mode = resolved.get("mode")
    seed = int(resolved.get("seed", 0))
    if mode == "smoke":
        user_count = int(resolved.get("smoke_user_count", 1000))
        selected = _stable_smoke_users(interactions, user_count, seed)
        interactions = [row for row in interactions if row.user_id in selected]
    elif mode != "full":
        raise DataQualityError("mode must be smoke or full")

    split_config = resolved.get("split", {})
    train, validation, test, split_quality = split_interactions(
        interactions,
        min_train_interactions=int(split_config.get("min_train_interactions", 1)),
    )
    time_decay = resolved.get("time_decay", {})
    popularity, reference_time = _popularity_rows(
        train,
        enabled=bool(time_decay.get("enabled", False)),
        half_life_seconds=time_decay.get("half_life_seconds"),
    )
    histories = _history_rows(train, validation, test)
    titles = _title_rows(items, train, validation, test)
    inspection = inspect_official_files(raw_path)
    source_files = [
        {
            key: value
            for key, value in inspection[name].items()
            if key in {"path", "size_bytes", "sha256", "rows"}
        }
        for name in ("pairs", "titles", "likes_views")
    ]
    config_checksum = sha256_bytes(canonical_json_bytes(resolved))
    identity = {
        "sources": [{"path": row["path"], "sha256": row["sha256"]} for row in source_files],
        "config_checksum": config_checksum,
        "seed": seed,
        "codec": codec.format_name,
    }
    data_version = f"microlens50k-{sha256_bytes(canonical_json_bytes(identity))[:16]}"
    output_path.mkdir(parents=True, exist_ok=True)
    temp_path = Path(tempfile.mkdtemp(prefix=f".{data_version}-", dir=output_path))
    artifacts: list[dict[str, Any]] = []
    try:
        tables: list[tuple[str, list[dict[str, Any]]]] = [
            ("items", [item.as_row() for item in items]),
            ("train", [row.as_row() for row in train]),
            ("validation", [row.as_row() for row in validation]),
            ("test", [row.as_row() for row in test]),
            ("user_history", histories),
            ("train_popularity", popularity),
            ("title_corpus", titles),
        ]
        for name, rows in tables:
            path = temp_path / f"{name}{codec.suffix}"
            count = codec.write_rows(path, rows)
            artifacts.append(artifact_descriptor(path, rows=count))
        quality_report = {
            "schema_version": "1.0",
            "data_version": data_version,
            "mode": mode,
            "counts": {
                "users": len({row.user_id for row in interactions}),
                "items": len(items),
                "interactions": len(interactions),
                "train": len(train),
                "validation": len(validation),
                "test": len(test),
            },
            "time_range": _range(interactions),
            "raw_quality": raw_quality,
            "split_quality": split_quality,
            "observed_anomalies": {
                "null_or_empty_required_values": 0,
                "invalid_timestamps": 0,
                "invalid_item_metadata": 0,
                "exact_duplicate_interactions": raw_quality["pairs"]["exact_duplicate_rows"],
                "orphan_interactions": raw_quality["orphan_interactions"],
            },
            "rules": {
                "duplicates": quality_config.get("duplicate_policy", "reject"),
                "orphans": quality_config.get("orphan_policy", "reject"),
                "null_required_fields": "reject",
                "invalid_timestamp": "reject",
                "timestamp_ties": "keep_group_in_one_split",
            },
            "snapshot_feature_status": "display_metadata_only_no_historical_snapshot_timestamp",
        }
        quality_path = temp_path / "quality_report.json"
        _write_json(quality_path, quality_report)
        artifacts.insert(0, artifact_descriptor(quality_path))
        manifest = {
            "schema_version": "1.0",
            "data_version": data_version,
            "source_urls": sorted(set(resolved.get("source_urls", []))),
            "source_files": source_files,
            "config_checksum": config_checksum,
            "seed": seed,
            "input_schema": {
                "pairs": ["user", "item", "timestamp"],
                "titles": ["item", "title"],
                "likes_views": ["item", "likes_snapshot", "views_snapshot"],
            },
            "output_schema": {
                "storage_format": codec.format_name,
                "writer_contract": {
                    "version": "phase-2a-v1",
                    "parquet_version": "2.6" if codec.suffix == ".parquet" else None,
                    "compression": "zstd_level_3" if codec.suffix == ".parquet" else None,
                    "dictionary_encoding": False if codec.suffix == ".parquet" else None,
                    "row_group_size": 65_536 if codec.suffix == ".parquet" else None,
                    "timestamp_timezone": "UTC",
                },
                "interaction": ["user_id", "item_id", "timestamp"],
                "item": [
                    "item_id",
                    "title",
                    "likes_snapshot",
                    "views_snapshot",
                    "cover_ref",
                    "metadata_status",
                ],
                "user_history": [
                    "user_id",
                    "ordered_item_ids",
                    "ordered_timestamps",
                    "split_cutoffs",
                ],
                "train_popularity": [
                    "item_id",
                    "count",
                    "probability",
                    "time_decayed_count",
                ],
                "title_corpus": [
                    "item_id",
                    "normalized_title",
                    "item_split_membership",
                    "is_train_item",
                ],
                "leakage_exclusions": ["likes_snapshot", "views_snapshot"],
            },
            "record_counts": {
                "users": len({row.user_id for row in interactions}),
                "items": len(items),
                "interactions": len(interactions),
                "train": len(train),
                "validation": len(validation),
                "test": len(test),
            },
            "time_ranges": {
                "source": _range(interactions),
                "train": _range(train),
                "validation": _range(validation),
                "test": _range(test),
            },
            "split_strategy": {
                "kind": "per_user_strict_time_holdout",
                "ordering_field": "timestamp",
                "leakage_checks": [
                    "whole_equal_timestamp_groups",
                    "max_train_lt_min_validation_lt_min_test_per_evaluation_user",
                ],
            },
            "low_interaction_policy": {
                "strategy": split_config.get("low_interaction", "train_only"),
                "minimum_distinct_timestamps_for_evaluation": 3,
                "minimum_train_interactions": int(split_config.get("min_train_interactions", 1)),
                "train_only_users": split_quality["train_only_users"],
            },
            "negative_sampling_candidate_policy": {
                "statistics_split": "train",
                "exclusion_history_split": "train",
                "available_strategies": ["uniform", "popularity_aware"],
                "popularity_alpha": float(
                    resolved.get("negative_sampling", {}).get("popularity_alpha", 0.75)
                ),
            },
            "time_decay": {
                "enabled": bool(time_decay.get("enabled", False)),
                "reference_time_utc": reference_time,
                "half_life_seconds": time_decay.get("half_life_seconds")
                if time_decay.get("enabled", False)
                else None,
                "statistics_split": "train",
            },
            "generation_command": canonical_json_bytes(
                {
                    "entrypoint": "recsys.data.build_official_dataset",
                    "parameters": {
                        "config_checksum": config_checksum,
                        "source_files": [
                            {"path": row["path"], "sha256": row["sha256"]} for row in source_files
                        ],
                        "seed": seed,
                        "codec": codec.format_name,
                    },
                }
            ).decode("utf-8"),
            "artifacts": artifacts,
        }
        manifest_path = temp_path / "manifest.json"
        _write_json(manifest_path, manifest)
        return _publish(temp_path, output_path / data_version)
    except Exception:
        if temp_path.exists():
            shutil.rmtree(temp_path)
        raise


__all__ = [
    "build_official_dataset",
    "inspect_official_files",
    "normalize_title",
    "split_interactions",
]
