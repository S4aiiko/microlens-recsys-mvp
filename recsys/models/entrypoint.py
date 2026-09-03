from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from recsys.data.artifacts import TableCodec
from recsys.data.common import canonical_json_bytes, sha256_file

from .artifacts import CandidateRankingArtifact, PublishedModelArtifacts, write_model_artifacts
from .badcases import build_badcases
from .baselines import evaluate_baselines_with_segments, relevant_by_user
from .config import load_model_config
from .data import ModelData, load_model_data, load_model_test_split
from .errors import ModelInputError, TrainingCancelled
from .evaluation import deepfm_rankings, dssm_rankings
from .features import FeatureIndex
from .metrics import aggregate_ranking_metrics, aggregate_segmented_ranking_metrics
from .training import (
    StageResult,
    build_deepfm,
    build_dssm,
    set_determinism,
    train_deepfm,
    train_dssm,
)


@dataclass(frozen=True, slots=True)
class TrainedModelStages:
    data: ModelData
    features: FeatureIndex
    config: dict[str, Any]
    config_checksum: str
    dssm: Any
    deepfm: Any
    dssm_stage: StageResult
    deepfm_stage: StageResult


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


def _evaluate(
    data: ModelData,
    features: FeatureIndex,
    config: Mapping[str, Any],
    dssm: Any,
    deepfm: Any,
    split: str = "test",
    include_baselines: bool = True,
    heartbeat: Callable[[], None] = lambda: None,
) -> tuple[dict[str, Any], CandidateRankingArtifact, list[dict[str, object]]]:
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
            CandidateRankingArtifact(candidates, scores),
            [],
        )
    k_values = [int(value) for value in config["evaluation"]["k"]]
    baselines: dict[str, Mapping[str, float]] = {}
    baseline_segments: dict[str, dict[str, dict[str, float | int]]] = {}
    if include_baselines:
        baselines, baseline_segments = evaluate_baselines_with_segments(
            data, split=split, k_values=k_values, seed=int(config["seed"])
        )
    relevant = relevant_by_user(getattr(data, split))
    candidates, scores = dssm_rankings(
        dssm, data, features, users=set(relevant), top_n=top_n, progress=heartbeat
    )
    dssm_metrics = aggregate_ranking_metrics(candidates, relevant, k_values)
    rankings = deepfm_rankings(deepfm, candidates, scores, features, progress=heartbeat)
    two_stage_metrics = aggregate_ranking_metrics(rankings, relevant, k_values)
    history_lengths = {user_id: len(data.user_train_items[user_id]) for user_id in relevant}
    segments = {
        **baseline_segments,
        "dssm": aggregate_segmented_ranking_metrics(
            candidates, relevant, history_lengths, k_values
        ),
        "two_stage": aggregate_segmented_ranking_metrics(
            rankings, relevant, history_lengths, k_values
        ),
    }
    badcases = (
        build_badcases(
            data,
            split=split,
            dssm_candidates=candidates,
            two_stage_rankings=rankings,
            maximum_rows=int(config["evaluation"].get("maximum_badcases", 200)),
        )
        if split == "test"
        else []
    )
    return (
        {
            **{name: dict(values) for name, values in baselines.items()},
            "dssm": dssm_metrics,
            "two_stage": two_stage_metrics,
            "segments": segments,
        },
        CandidateRankingArtifact(candidates, scores),
        badcases,
    )


def train_model_stages(
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
) -> TrainedModelStages:
    """Train both stages with validation-only early stopping and no final-test read."""

    resolved_config, config_checksum = load_model_config(config)
    data = load_model_data(
        processed_root=processed_root,
        data_version=data_version,
        data_manifest_checksum=data_manifest_checksum,
        title_config=resolved_config["title"],
        codec=codec,
        include_test=False,
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
        raise TrainingCancelled("training cancelled after DeepFM")
    return TrainedModelStages(
        data=data,
        features=features,
        config=resolved_config,
        config_checksum=config_checksum,
        dssm=dssm,
        deepfm=deepfm,
        dssm_stage=dssm_stage,
        deepfm_stage=deepfm_stage,
    )


def evaluate_validation_selection(
    trained: TrainedModelStages,
    *,
    heartbeat: Callable[[], None] = lambda: None,
    cancellation_requested: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Evaluate a trained matrix candidate on validation without reading test metrics."""

    def validation_pulse() -> None:
        if cancellation_requested():
            raise TrainingCancelled("training cancelled during validation selection")
        heartbeat()

    metrics, _candidates, _badcases = _evaluate(
        trained.data,
        trained.features,
        trained.config,
        trained.dssm,
        trained.deepfm,
        split="validation",
        include_baselines=False,
        heartbeat=validation_pulse,
    )
    return metrics


def load_trained_model_test_split(
    trained: TrainedModelStages,
    *,
    processed_root: str | Path,
    codec: TableCodec | None = None,
) -> TrainedModelStages:
    """Return the trained stages with their one immutable final-test cohort attached."""

    data = load_model_test_split(trained.data, processed_root=processed_root, codec=codec)
    return replace(trained, data=data)


def finalize_trained_model(
    trained: TrainedModelStages,
    *,
    output_root: str | Path,
    heartbeat: Callable[[], None] = lambda: None,
    cancellation_requested: Callable[[], bool] = lambda: False,
    git_revision: str | None = None,
) -> PublishedModelArtifacts:
    """Evaluate a test split that the caller explicitly loaded after selection."""

    if not trained.data.test_loaded:
        raise ModelInputError("final evaluation requires the explicitly loaded test split")

    def final_evaluation_pulse() -> None:
        if cancellation_requested():
            raise TrainingCancelled("training cancelled during final evaluation")
        heartbeat()

    metrics, candidates, badcases = _evaluate(
        trained.data,
        trained.features,
        trained.config,
        trained.dssm,
        trained.deepfm,
        split="test",
        include_baselines=True,
        heartbeat=final_evaluation_pulse,
    )
    return write_model_artifacts(
        output_root=output_root,
        data=trained.data,
        config=trained.config,
        config_checksum=trained.config_checksum,
        dssm=trained.dssm,
        deepfm=trained.deepfm,
        dssm_stage=trained.dssm_stage,
        deepfm_stage=trained.deepfm_stage,
        metrics=metrics,
        badcases=badcases,
        candidates=candidates,
        git_revision=git_revision,
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
    """Train both stages and finalize one model on the test split."""

    trained = train_model_stages(
        processed_root=processed_root,
        data_version=data_version,
        data_manifest_checksum=data_manifest_checksum,
        config=config,
        output_root=output_root,
        checkpoint_root=checkpoint_root,
        resume_dssm=resume_dssm,
        resume_deepfm=resume_deepfm,
        heartbeat=heartbeat,
        cancellation_requested=cancellation_requested,
        codec=codec,
    )
    trained = load_trained_model_test_split(
        trained,
        processed_root=processed_root,
        codec=codec,
    )
    return finalize_trained_model(
        trained,
        output_root=output_root,
        heartbeat=heartbeat,
        cancellation_requested=cancellation_requested,
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
