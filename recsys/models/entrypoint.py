from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from recsys.data.artifacts import TableCodec
from recsys.data.common import canonical_json_bytes, sha256_file

from .artifacts import PublishedModelArtifacts, write_model_artifacts
from .badcases import build_badcases
from .baselines import evaluate_baselines
from .config import load_model_config
from .data import ModelData, load_model_data
from .errors import ModelInputError, TrainingCancelled
from .evaluation import dssm_rankings, evaluate_dssm, evaluate_two_stage
from .features import FeatureIndex
from .training import build_deepfm, build_dssm, set_determinism, train_deepfm, train_dssm


def _write_checkpoint(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ModelInputError("checkpoint directory must not be a symlink")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(document) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_checkpoint(path: str | Path | None, *, stage: str) -> dict[str, Any] | None:
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ModelInputError(f"{stage} resume checkpoint is missing or unsafe")
    try:
        document = json.loads(candidate.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelInputError(f"{stage} resume checkpoint is invalid") from exc
    if not isinstance(document, dict) or document.get("stage") != stage:
        raise ModelInputError(f"{stage} resume checkpoint has the wrong stage")
    return document


def _candidate_document(
    rankings: Mapping[str, list[str]], scores: Mapping[str, Mapping[str, float]]
) -> dict[str, list[dict[str, object]]]:
    return {
        user_id: [
            {"item_id": item_id, "rank": rank, "score": float(scores[user_id][item_id])}
            for rank, item_id in enumerate(items, start=1)
        ]
        for user_id, items in sorted(rankings.items())
    }


def _evaluate(
    data: ModelData,
    features: FeatureIndex,
    config: Mapping[str, Any],
    dssm: Any,
    deepfm: Any,
    heartbeat: Callable[[], None] = lambda: None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    top_n = int(config["dssm"]["candidate_top_n"])
    if data.purpose == "systems_only":
        users = set(data.user_ids)
        candidates, scores = dssm_rankings(
            dssm, data, features, users=users, top_n=top_n, progress=heartbeat
        )
        return (
            {
                "systems_evidence": {
                    "trained_interactions": len(data.train),
                    "generated_candidate_users": len(candidates),
                    "event_training_signals_consumed": len(data.event_training_signals),
                }
            },
            _candidate_document(candidates, scores),
            [],
        )
    k_values = [int(value) for value in config["evaluation"]["k"]]
    baselines = evaluate_baselines(data, split="test", k_values=k_values, seed=int(config["seed"]))
    dssm_metrics, candidates, scores = evaluate_dssm(
        dssm,
        data,
        features,
        split="test",
        k_values=k_values,
        top_n=top_n,
        progress=heartbeat,
    )
    two_stage_metrics, rankings, _recall_scores = evaluate_two_stage(
        dssm,
        deepfm,
        data,
        features,
        split="test",
        k_values=k_values,
        candidate_top_n=top_n,
        progress=heartbeat,
    )
    limited_candidates = {user_id: items[:top_n] for user_id, items in candidates.items()}
    candidate_scores = {
        user_id: {item_id: scores[user_id][item_id] for item_id in items}
        for user_id, items in limited_candidates.items()
    }
    badcases = build_badcases(
        data,
        split="test",
        dssm_candidates=limited_candidates,
        two_stage_rankings=rankings,
        maximum_rows=int(config["evaluation"].get("maximum_badcases", 200)),
    )
    return (
        {
            "random": dict(baselines["random"]),
            "popularity": dict(baselines["popularity"]),
            "dssm": dssm_metrics,
            "two_stage": two_stage_metrics,
        },
        _candidate_document(limited_candidates, candidate_scores),
        badcases,
    )


def train_model(
    *,
    processed_root: str | Path,
    data_version: str,
    data_manifest_checksum: str,
    config: Mapping[str, Any] | str | Path,
    output_root: str | Path,
    checkpoint_root: str | Path | None = None,
    resume_dssm: str | Path | None = None,
    resume_deepfm: str | Path | None = None,
    heartbeat: Callable[[], None] = lambda: None,
    cancellation_requested: Callable[[], bool] = lambda: False,
    codec: TableCodec | None = None,
) -> PublishedModelArtifacts:
    """Train both stages from one explicit immutable data version and write a bundle."""

    resolved_config, config_checksum = load_model_config(config)
    data = load_model_data(
        processed_root=processed_root,
        data_version=data_version,
        data_manifest_checksum=data_manifest_checksum,
        title_config=resolved_config["title"],
        codec=codec,
    )
    set_determinism(int(resolved_config["seed"]))
    features = FeatureIndex.build(data, title_enabled=bool(resolved_config["title"]["enabled"]))
    dssm = build_dssm(data, resolved_config)
    deepfm = build_deepfm(data, resolved_config)
    checkpoints = Path(checkpoint_root or Path(output_root) / ".checkpoints")
    checkpoint_identity = f"{data.data_version}-{config_checksum[:16]}"

    def save_checkpoint(stage: str, document: dict[str, Any]) -> None:
        enriched = {
            **document,
            "data_version": data.data_version,
            "data_manifest_checksum": data.manifest_checksum,
            "config_checksum": config_checksum,
        }
        _write_checkpoint(checkpoints / checkpoint_identity / f"{stage}.json", enriched)

    dssm_resume = _read_checkpoint(resume_dssm, stage="dssm")
    deepfm_resume = _read_checkpoint(resume_deepfm, stage="deepfm")
    for resume in (dssm_resume, deepfm_resume):
        if resume is not None and (
            resume.get("data_version") != data.data_version
            or resume.get("data_manifest_checksum") != data.manifest_checksum
            or resume.get("config_checksum") != config_checksum
        ):
            raise ModelInputError("resume checkpoint identity does not match this training run")
    dssm_stage = train_dssm(
        dssm,
        data,
        features,
        resolved_config,
        checkpoint=save_checkpoint,
        heartbeat=heartbeat,
        cancellation_requested=cancellation_requested,
        resume=dssm_resume,
    )
    if cancellation_requested():
        raise TrainingCancelled("training cancelled between DSSM and DeepFM")
    deepfm_stage = train_deepfm(
        deepfm,
        dssm,
        data,
        features,
        resolved_config,
        checkpoint=save_checkpoint,
        heartbeat=heartbeat,
        cancellation_requested=cancellation_requested,
        resume=deepfm_resume,
    )
    if cancellation_requested():
        raise TrainingCancelled("training cancelled before final evaluation")

    def final_evaluation_pulse() -> None:
        if cancellation_requested():
            raise TrainingCancelled("training cancelled during final evaluation")
        heartbeat()

    metrics, candidates, badcases = _evaluate(
        data,
        features,
        resolved_config,
        dssm,
        deepfm,
        heartbeat=final_evaluation_pulse,
    )
    return write_model_artifacts(
        output_root=output_root,
        data=data,
        config=resolved_config,
        config_checksum=config_checksum,
        dssm=dssm,
        deepfm=deepfm,
        dssm_stage=dssm_stage,
        deepfm_stage=deepfm_stage,
        metrics=metrics,
        badcases=badcases,
        candidates=candidates,
    )


def worker_training_handler(request: Any, control: Any) -> dict[str, Any]:
    """Stable worker entrypoint; training never publishes or activates a model."""

    config_path = os.environ.get("WORKER_MODEL_CONFIG")
    processed_root = os.environ.get("PROCESSED_DATA_ROOT")
    output_root = os.environ.get("MODEL_ARTIFACT_ROOT")
    if not config_path or not processed_root or not output_root:
        raise ModelInputError(
            "WORKER_MODEL_CONFIG, PROCESSED_DATA_ROOT and MODEL_ARTIFACT_ROOT are required"
        )
    _config, actual_config_checksum = load_model_config(config_path)
    if actual_config_checksum != str(request.config_checksum):
        raise ModelInputError("worker config checksum does not match the training job")
    artifact = train_model(
        processed_root=processed_root,
        data_version=str(request.data_version),
        data_manifest_checksum=str(request.data_manifest_checksum),
        config=config_path,
        output_root=output_root,
        heartbeat=control.heartbeat,
        cancellation_requested=control.cancellation_requested,
    )
    manifest = artifact.manifest
    requested_identity = (
        str(request.purpose),
        str(request.evaluation_comparability),
        bool(request.activation_eligible),
    )
    actual_identity = (
        str(manifest["purpose"]),
        str(manifest["evaluation_comparability"]),
        bool(manifest["activation_eligible"]),
    )
    if requested_identity != actual_identity:
        raise ModelInputError("worker job policy does not match the immutable data manifest")
    return {
        "model_version": artifact.model_version,
        "manifest_checksum": artifact.manifest_checksum,
        "config_checksum": actual_config_checksum,
        "artifact_checksum": sha256_file(artifact.bundle_path),
        "artifact_uri": f"{artifact.model_version}/bundle.json",
        "model_status": artifact.status,
        "activation_eligible": bool(manifest["activation_eligible"]),
        "published": False,
        "activated": False,
    }
