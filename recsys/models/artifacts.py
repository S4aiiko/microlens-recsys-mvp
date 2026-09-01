from __future__ import annotations

import csv
import io
import json
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch

from recsys.data.common import (
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    sha256_bytes,
    sha256_file,
    validate_relative_file_name,
)

from .bundle import MAX_BUNDLE_BYTES, load_bundle
from .data import ModelData, data_lineage
from .deepfm import DeepFMRanker
from .errors import ModelArtifactError
from .state import encode_state_dict, encode_tensor
from .training import StageResult
from .two_tower import TwoTowerModel


@dataclass(frozen=True, slots=True)
class PublishedModelArtifacts:
    model_version: str
    path: Path
    bundle_path: Path
    bundle_checksum: str
    manifest: dict[str, Any]
    manifest_checksum: str
    status: str


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, canonical_json_bytes(value) + b"\n")


def _descriptor(path: Path, *, shape: list[int] | None, dtype: str | None) -> dict[str, Any]:
    return {
        "path": path.name,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "shape": shape,
        "dtype": dtype,
    }


def _csv_bytes(fieldnames: list[str], rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fieldnames})
    return output.getvalue().encode("utf-8")


def _flatten_metrics(metrics: dict[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for group, values in metrics.items():
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                output[f"{group}.{name}"] = float(value)
    return dict(sorted(output.items()))


def _verify_existing(path: Path) -> PublishedModelArtifacts:
    if path.is_symlink() or not path.is_dir():
        raise ModelArtifactError("existing model version is not a real directory")
    manifest_path = path / "manifest.json"
    bundle_path = path / "bundle.json"
    if any(
        candidate.is_symlink() or not candidate.is_file()
        for candidate in (manifest_path, bundle_path)
    ):
        raise ModelArtifactError("existing model version is incomplete or unsafe")
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelArtifactError("existing model manifest is invalid") from exc
    checksum = sha256_bytes(manifest_bytes)
    load_bundle(bundle_path, checksum)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ModelArtifactError("existing model manifest has no artifact inventory")
    seen: set[str] = set()
    for descriptor in artifacts:
        if not isinstance(descriptor, dict):
            raise ModelArtifactError("existing model artifact descriptor is invalid")
        try:
            name = validate_relative_file_name(descriptor.get("path"))
        except ValueError as exc:
            raise ModelArtifactError("existing model artifact path is unsafe") from exc
        if name in seen:
            raise ModelArtifactError("existing model artifact inventory has duplicates")
        seen.add(name)
        candidate = path / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ModelArtifactError(f"existing model artifact is missing or unsafe: {name}")
        if candidate.stat().st_size != descriptor.get("size_bytes"):
            raise ModelArtifactError(f"existing model artifact size mismatch: {name}")
        if sha256_file(candidate) != descriptor.get("sha256"):
            raise ModelArtifactError(f"existing model artifact checksum mismatch: {name}")
    return PublishedModelArtifacts(
        model_version=str(manifest["model_version"]),
        path=path,
        bundle_path=bundle_path,
        bundle_checksum=sha256_file(bundle_path),
        manifest=manifest,
        manifest_checksum=checksum,
        status=str(manifest["status"]),
    )


def write_model_artifacts(
    *,
    output_root: str | Path,
    data: ModelData,
    config: dict[str, Any],
    config_checksum: str,
    dssm: TwoTowerModel,
    deepfm: DeepFMRanker,
    dssm_stage: StageResult,
    deepfm_stage: StageResult,
    metrics: dict[str, Any],
    badcases: list[dict[str, Any]],
    candidates: dict[str, list[dict[str, Any]]],
) -> PublishedModelArtifacts:
    dssm_state = encode_state_dict(dssm.state_dict())
    deepfm_state = encode_state_dict(deepfm.state_dict())
    stage_execution_checksum = sha256_bytes(
        canonical_json_bytes(
            {
                "dssm": {
                    "best_epoch": dssm_stage.best_epoch,
                    "stop_reason": dssm_stage.stop_reason,
                    "history": dssm_stage.history,
                    "resumed_from_epoch": dssm_stage.resumed_from_epoch,
                },
                "deepfm": {
                    "best_epoch": deepfm_stage.best_epoch,
                    "stop_reason": deepfm_stage.stop_reason,
                    "history": deepfm_stage.history,
                    "resumed_from_epoch": deepfm_stage.resumed_from_epoch,
                },
            }
        )
    )
    model_identity = {
        "schema_version": "1.0",
        "data_version": data.data_version,
        "data_manifest_checksum": data.manifest_checksum,
        "resolved_config_checksum": config_checksum,
        "seed": int(config["seed"]),
        "title_encoder_checksum": data.title_encoder.checksum,
        "user_ids_checksum": sha256_bytes(canonical_json_bytes(list(data.user_ids))),
        "item_ids_checksum": sha256_bytes(canonical_json_bytes(list(data.item_ids))),
        "train_popularity_checksum": sha256_bytes(
            canonical_json_bytes(dict(sorted(data.train_popularity.items())))
        ),
        "dssm_state_checksum": sha256_bytes(canonical_json_bytes(dssm_state)),
        "deepfm_state_checksum": sha256_bytes(canonical_json_bytes(deepfm_state)),
        "metrics_checksum": sha256_bytes(canonical_json_bytes(metrics)),
        "stage_execution_checksum": stage_execution_checksum,
    }
    model_version = f"model-{sha256_bytes(canonical_json_bytes(model_identity))[:20]}"
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ModelArtifactError("model artifact root must not be a symlink")
    final_path = root / model_version
    if final_path.exists() or final_path.is_symlink():
        existing = _verify_existing(final_path)
        if existing.manifest.get("resolved_config_checksum") != config_checksum:
            raise ModelArtifactError("model version collision")
        return existing
    temporary = Path(tempfile.mkdtemp(prefix=f".{model_version}-", dir=root))
    try:
        item_embeddings = dssm.all_item_embeddings().cpu()
        files: list[dict[str, Any]] = []
        documents = {
            "resolved_config.json": config,
            "title_encoder.json": data.title_encoder.as_dict(),
            "dssm_checkpoint.json": {
                "schema_version": "1.0",
                "stage": "dssm",
                "best_epoch": dssm_stage.best_epoch,
                "stop_reason": dssm_stage.stop_reason,
                "state": dssm_state,
            },
            "deepfm_checkpoint.json": {
                "schema_version": "1.0",
                "stage": "deepfm",
                "best_epoch": deepfm_stage.best_epoch,
                "stop_reason": deepfm_stage.stop_reason,
                "state": deepfm_state,
            },
            "item_embeddings.json": encode_tensor(item_embeddings),
            "item_ids.json": list(data.item_ids),
            "metrics.json": metrics,
            "stage_training.json": {
                "dssm": {
                    "best_epoch": dssm_stage.best_epoch,
                    "stop_reason": dssm_stage.stop_reason,
                    "best_validation_metric": dssm_stage.best_validation_metric,
                    "history": list(dssm_stage.history),
                    "resumed_from_epoch": dssm_stage.resumed_from_epoch,
                },
                "deepfm": {
                    "best_epoch": deepfm_stage.best_epoch,
                    "stop_reason": deepfm_stage.stop_reason,
                    "best_validation_metric": deepfm_stage.best_validation_metric,
                    "history": list(deepfm_stage.history),
                    "resumed_from_epoch": deepfm_stage.resumed_from_epoch,
                },
                "event_training_signals_consumed": len(data.event_training_signals),
            },
            "dssm_candidates.json": candidates,
        }
        for filename, document in documents.items():
            path = temporary / filename
            _write_json(path, document)
            shape: list[int] | None = None
            dtype: str | None = "json"
            if filename == "item_embeddings.json":
                shape = list(item_embeddings.shape)
                dtype = str(item_embeddings.dtype).removeprefix("torch.")
            elif filename == "item_ids.json":
                shape = [len(data.item_ids)]
                dtype = "string"
            elif filename == "title_encoder.json":
                shape = [data.title_encoder.bucket_count]
                dtype = "int64_document_frequency"
            files.append(_descriptor(path, shape=shape, dtype=dtype))
        metric_rows = [
            {"metric": name, "value": value} for name, value in _flatten_metrics(metrics).items()
        ]
        metrics_csv = temporary / "metrics.csv"
        _write_bytes(metrics_csv, _csv_bytes(["metric", "value"], metric_rows))
        files.append(_descriptor(metrics_csv, shape=[len(metric_rows), 2], dtype="csv"))
        badcase_csv = temporary / "badcases.csv"
        badcase_fields = [
            "user_id",
            "item_id",
            "category",
            "dssm_candidate_found",
            "two_stage_rank",
            "history_length",
            "title_length",
        ]
        _write_bytes(badcase_csv, _csv_bytes(badcase_fields, badcases))
        files.append(
            _descriptor(badcase_csv, shape=[len(badcases), len(badcase_fields)], dtype="csv")
        )
        purpose = data.purpose
        eligible = (
            data.activation_eligible
            and data.evaluation_comparability == "comparable"
            and purpose != "systems_only"
        )
        status = "READY" if eligible else "EVALUATED"
        evaluation_metrics = {} if not eligible else _flatten_metrics(metrics)
        manifest: dict[str, Any] = {
            "schema_version": "1.0",
            "model_version": model_version,
            "data_version": data.data_version,
            "data_manifest_checksum": data.manifest_checksum,
            "purpose": purpose,
            "evaluation_comparability": data.evaluation_comparability,
            "activation_eligible": data.activation_eligible,
            "git_revision": os.environ.get("GIT_REVISION") or None,
            "algorithms": ["random", "popularity", "dssm", "deepfm"],
            "features": list(
                dict.fromkeys(
                    [
                        "user_id",
                        "item_id",
                        *(
                            ["train_history_title_hash_embedding_bag"]
                            if config["title"]["enabled"]
                            else ["title_features_disabled_zero_control"]
                        ),
                        *[
                            name
                            for name in config["deepfm"].get("dense_features", [])
                            if config["title"]["enabled"] or name != "title_history_similarity"
                        ],
                    ]
                )
            ),
            "resolved_config_checksum": config_checksum,
            "model_identity": model_identity,
            "seed": int(config["seed"]),
            "negative_sampling": config["dssm"]["negative_sampling"],
            "time_decay": config["dssm"]["time_decay"],
            "best_epoch": deepfm_stage.best_epoch,
            "early_stop_reason": deepfm_stage.stop_reason,
            "evaluation": {
                "split": "test",
                "candidate_policy": (
                    config["evaluation"]["candidate_policy"]
                    if purpose != "systems_only"
                    else "not_evaluated_systems_only"
                ),
                "k": [int(value) for value in config["evaluation"]["k"]],
                "metrics": evaluation_metrics,
            },
            "artifacts": files,
            "generation_command": canonical_json_bytes(
                {
                    "entrypoint": "recsys.models.entrypoint.train_model",
                    "parameters": {
                        "data_version": data.data_version,
                        "data_manifest_checksum": data.manifest_checksum,
                        "config_checksum": config_checksum,
                    },
                }
            ).decode("utf-8"),
            "runtime_environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "torch": torch.__version__,
                "device": "cpu",
                "title_encoder_checksum": data.title_encoder.checksum,
                "dssm_best_epoch": dssm_stage.best_epoch,
                "dssm_stop_reason": dssm_stage.stop_reason,
                "deepfm_best_epoch": deepfm_stage.best_epoch,
                "deepfm_stop_reason": deepfm_stage.stop_reason,
                "event_training_signals_consumed": len(data.event_training_signals),
            },
            "status": status,
            "failure_reason": None,
        }
        lineage = data_lineage(data)
        for field in (
            "base_data_version",
            "event_export_checksum",
            "event_id_watermark_range",
            "event_mapping_config_checksum",
        ):
            if field in lineage:
                manifest[field] = lineage[field]
        if purpose == "quality_evaluation":
            manifest["frozen_windows"] = {
                "train_cutoff_utc": lineage.get("train_cutoff_utc"),
                "validation_window_utc": lineage.get("validation_window_utc"),
                "test_window_utc": lineage.get("test_window_utc"),
            }
        manifest_path = temporary / "manifest.json"
        _write_json(manifest_path, manifest)
        manifest_checksum = sha256_file(manifest_path)
        evidence_kind = (
            "official_smoke_two_stage"
            if purpose == "base_official" and config.get("mode") == "smoke"
            else "base_official_two_stage"
            if purpose == "base_official"
            else "quality_evaluation_two_stage"
            if purpose == "quality_evaluation"
            else "systems_only_two_stage"
        )
        bundle_document = {
            "schema_version": "1.0",
            "model_version": model_version,
            "data_version": data.data_version,
            "manifest_checksum": manifest_checksum,
            "config_checksum": config_checksum,
            "fixture_evidence": {"kind": evidence_kind, "dssm": True, "deepfm": True},
            "manifest": manifest,
            "resolved_config": config,
            "user_ids": list(data.user_ids),
            "item_ids": list(data.item_ids),
            "title_encoder": data.title_encoder.as_dict(),
            "train_popularity": dict(sorted(data.train_popularity.items())),
            "dssm_state": dssm_state,
            "deepfm_state": deepfm_state,
            "metrics": metrics,
        }
        bundle_path = temporary / "bundle.json"
        _write_json(bundle_path, bundle_document)
        if bundle_path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ModelArtifactError("ModelBundle exceeds the formal 7B staging limit")
        load_bundle(bundle_path, manifest_checksum)
        for path in temporary.iterdir():
            if path.is_file():
                fsync_file(path)
        fsync_directory(temporary)
        temporary.replace(final_path)
        fsync_directory(root)
        return _verify_existing(final_path)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
