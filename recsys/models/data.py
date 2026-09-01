from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recsys.data.artifacts import ParquetCodec, TableCodec
from recsys.data.common import (
    SHA256_PATTERN,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    utc_to_epoch_ms,
    validate_artifact_descriptor,
    validate_relative_file_name,
)

from .errors import ModelInputError
from .text import EncodedTitle, TitleHashEncoder, merge_encoded_titles


@dataclass(frozen=True, slots=True)
class ModelData:
    data_version: str
    manifest_checksum: str
    manifest: dict[str, Any]
    purpose: str
    evaluation_comparability: str
    activation_eligible: bool
    item_ids: tuple[str, ...]
    user_ids: tuple[str, ...]
    titles: Mapping[str, str]
    train: tuple[dict[str, Any], ...]
    validation: tuple[dict[str, Any], ...]
    test: tuple[dict[str, Any], ...]
    train_popularity: Mapping[str, float]
    user_train_items: Mapping[str, tuple[str, ...]]
    user_history_titles: Mapping[str, EncodedTitle]
    encoded_titles: Mapping[str, EncodedTitle]
    title_encoder: TitleHashEncoder
    event_training_signals: tuple[dict[str, Any], ...]


def _read_manifest(version_path: Path, expected_checksum: str) -> tuple[dict[str, Any], str]:
    if version_path.is_symlink() or not version_path.is_dir():
        raise ModelInputError("data version must be a real immutable directory")
    manifest_path = version_path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ModelInputError("data manifest is missing or unsafe")
    payload = manifest_path.read_bytes()
    actual_checksum = sha256_bytes(payload)
    if actual_checksum != expected_checksum:
        raise ModelInputError("data manifest checksum mismatch")
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelInputError("data manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
        raise ModelInputError("unsupported data manifest")
    if manifest.get("data_version") != version_path.name:
        raise ModelInputError("data manifest version/path mismatch")
    return manifest, actual_checksum


def _artifact_map(version_path: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ModelInputError("data manifest artifacts must be an array")
    output: dict[str, dict[str, Any]] = {}
    for raw in raw_artifacts:
        try:
            descriptor = validate_artifact_descriptor(raw)
        except ValueError as exc:
            raise ModelInputError(f"invalid data artifact descriptor: {exc}") from exc
        name = descriptor["path"]
        if name in output:
            raise ModelInputError("duplicate data artifact path")
        path = version_path / name
        if path.is_symlink() or not path.is_file():
            raise ModelInputError(f"data artifact is missing or unsafe: {name}")
        if (
            path.stat().st_size != descriptor["size_bytes"]
            or sha256_file(path) != descriptor["sha256"]
        ):
            raise ModelInputError(f"data artifact checksum mismatch: {name}")
        output[name] = descriptor
    return output


def _read_table(
    version_path: Path,
    artifacts: Mapping[str, dict[str, Any]],
    codec: TableCodec,
    name: str,
    *,
    required: bool = True,
) -> list[dict[str, Any]]:
    filename = f"{name}{codec.suffix}"
    descriptor = artifacts.get(filename)
    if descriptor is None:
        if required:
            raise ModelInputError(f"required data artifact is missing: {filename}")
        return []
    path = version_path / filename
    rows = codec.read_rows(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != descriptor["size_bytes"]
        or sha256_file(path) != descriptor["sha256"]
    ):
        raise ModelInputError(f"data artifact changed while read: {filename}")
    if "rows" in descriptor and len(rows) != descriptor["rows"]:
        raise ModelInputError(f"data artifact row count mismatch: {filename}")
    return rows


def load_model_data(
    *,
    processed_root: str | Path,
    data_version: str,
    data_manifest_checksum: str,
    title_config: Mapping[str, Any],
    codec: TableCodec | None = None,
) -> ModelData:
    try:
        safe_version = validate_relative_file_name(data_version)
    except ValueError as exc:
        raise ModelInputError("data_version must be one explicit immutable name") from exc
    if safe_version.lower() == "latest":
        raise ModelInputError("data_version must never be latest")
    if not SHA256_PATTERN.fullmatch(data_manifest_checksum):
        raise ModelInputError("data_manifest_checksum must be lowercase SHA-256")
    if codec is None:
        codec = ParquetCodec()
        codec.validate_runtime()
    version_path = Path(processed_root) / safe_version
    manifest, manifest_checksum = _read_manifest(version_path, data_manifest_checksum)
    if manifest.get("output_schema", {}).get("storage_format") != codec.format_name:
        raise ModelInputError("data storage codec does not match the requested runtime")
    artifacts = _artifact_map(version_path, manifest)
    items = _read_table(version_path, artifacts, codec, "items")
    train = _read_table(version_path, artifacts, codec, "train")
    validation = _read_table(version_path, artifacts, codec, "validation")
    test = _read_table(version_path, artifacts, codec, "test")
    popularity_rows = _read_table(version_path, artifacts, codec, "train_popularity")
    title_rows = _read_table(version_path, artifacts, codec, "title_corpus")
    event_signals = _read_table(
        version_path, artifacts, codec, "event_training_signals", required=False
    )
    purpose = str(manifest.get("purpose", "base_official"))
    if not train:
        raise ModelInputError("model training requires a non-empty train split")
    if purpose != "systems_only" and (not validation or not test):
        raise ModelInputError("model quality training requires non-empty validation/test splits")
    item_ids = tuple(str(row["item_id"]) for row in items)
    if len(item_ids) != len(set(item_ids)):
        raise ModelInputError("item catalog contains duplicate IDs")
    titles = {str(row["item_id"]): str(row["normalized_title"]) for row in title_rows}
    if set(titles) != set(item_ids):
        raise ModelInputError("title corpus must exactly cover the item catalog")
    train_item_ids = {str(row["item_id"]) for row in train}
    declared_train_item_ids = {
        str(row["item_id"]) for row in title_rows if row.get("is_train_item") is True
    }
    if train_item_ids != declared_train_item_ids:
        raise ModelInputError("title train membership does not match train interactions")
    train_titles = {item_id: titles[item_id] for item_id in train_item_ids}
    encoder = TitleHashEncoder.fit(
        train_titles,
        bucket_count=int(title_config["bucket_count"]),
        ngram_min=int(title_config.get("ngram_min", 1)),
        ngram_max=int(title_config.get("ngram_max", 2)),
    )
    encoded_titles = encoder.transform_many(titles)
    histories: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in train:
        histories[str(row["user_id"])].append((int(row["timestamp"]), str(row["item_id"])))
    user_train_items = {
        user_id: tuple(item_id for _time, item_id in sorted(rows))
        for user_id, rows in sorted(histories.items())
    }
    user_history_titles = {
        user_id: merge_encoded_titles(encoded_titles[item_id] for item_id in history)
        for user_id, history in user_train_items.items()
    }
    popularity = {str(row["item_id"]): float(row["time_decayed_count"]) for row in popularity_rows}
    comparability = str(
        manifest.get(
            "evaluation_comparability",
            "comparable" if purpose == "base_official" else "non_comparable",
        )
    )
    activation_eligible = bool(manifest.get("activation_eligible", purpose == "base_official"))
    if purpose == "systems_only" and (comparability != "non_comparable" or activation_eligible):
        raise ModelInputError("systems_only data must be non-comparable and ineligible")
    if comparability == "non_comparable" and activation_eligible:
        raise ModelInputError("non-comparable data cannot be activation eligible")
    if purpose == "quality_evaluation" and not activation_eligible:
        raise ModelInputError("quality_evaluation data did not pass its frozen holdout gate")
    if purpose == "quality_evaluation":
        try:
            train_cutoff = utc_to_epoch_ms(str(manifest["train_cutoff_utc"]))
            validation_window = manifest["validation_window_utc"]
            test_window = manifest["test_window_utc"]
            validation_start = utc_to_epoch_ms(str(validation_window["from_utc"]))
            validation_end = utc_to_epoch_ms(str(validation_window["to_utc"]))
            test_start = utc_to_epoch_ms(str(test_window["from_utc"]))
            test_end = utc_to_epoch_ms(str(test_window["to_utc"]))
            holdout_counts = manifest["holdout_counts"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelInputError("quality_evaluation requires valid frozen windows") from exc
        if not train_cutoff <= validation_start < validation_end <= test_start < test_end:
            raise ModelInputError("quality_evaluation frozen windows are not strictly ordered")
        if any(int(row["timestamp"]) >= train_cutoff for row in train):
            raise ModelInputError("quality_evaluation train split crosses its frozen cutoff")
        if any(
            not validation_start <= int(row["timestamp"]) < validation_end for row in validation
        ):
            raise ModelInputError("validation interactions fall outside the frozen later window")
        if any(not test_start <= int(row["timestamp"]) < test_end for row in test):
            raise ModelInputError("test interactions fall outside the frozen latest window")
        if (
            not isinstance(holdout_counts, dict)
            or int(holdout_counts.get("validation", -1)) != len(validation)
            or int(holdout_counts.get("test", -1)) != len(test)
        ):
            raise ModelInputError("quality_evaluation holdout counts do not match artifacts")
    users = tuple(sorted(user_train_items))
    evaluation_users = {str(row["user_id"]) for row in validation + test}
    if not evaluation_users <= set(users):
        raise ModelInputError("validation/test users must have train history")
    return ModelData(
        data_version=safe_version,
        manifest_checksum=manifest_checksum,
        manifest=manifest,
        purpose=purpose,
        evaluation_comparability=comparability,
        activation_eligible=activation_eligible,
        item_ids=item_ids,
        user_ids=users,
        titles=titles,
        train=tuple(train),
        validation=tuple(validation),
        test=tuple(test),
        train_popularity=popularity,
        user_train_items=user_train_items,
        user_history_titles=user_history_titles,
        encoded_titles=encoded_titles,
        title_encoder=encoder,
        event_training_signals=tuple(event_signals),
    )


def data_lineage(data: ModelData) -> dict[str, Any]:
    manifest = data.manifest
    lineage = {
        "data_version": data.data_version,
        "data_manifest_checksum": data.manifest_checksum,
        "purpose": data.purpose,
        "evaluation_comparability": data.evaluation_comparability,
        "activation_eligible": data.activation_eligible,
    }
    for field in (
        "base_data_version",
        "event_export_checksum",
        "event_id_watermark_range",
        "event_mapping_config_checksum",
        "train_cutoff_utc",
        "validation_window_utc",
        "test_window_utc",
    ):
        if field in manifest:
            lineage[field] = manifest[field]
    lineage["lineage_checksum"] = sha256_bytes(canonical_json_bytes(lineage))
    return lineage
