from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
import socket
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from recsys.data.common import (
    SHA256_PATTERN,
    canonical_json_bytes,
    fsync_directory,
    sha256_bytes,
    sha256_file,
    validate_relative_file_name,
)
from recsys.models.config import load_model_config
from recsys.models.errors import ModelInputError
from recsys.models.metrics import ACTIVITY_SEGMENTS, RankingMetricAccumulator, activity_segment

from .source_identity import load_attestation, source_checksum

if TYPE_CHECKING:
    from recsys.models.entrypoint import TrainedModelStages

_MATRIX_KEYS = {
    "schema_version",
    "phase",
    "base_config",
    "execution_policy",
    "selection",
    "model_experiments",
    "serving_ablation",
}
_MODEL_EXPERIMENT_KEYS = {"experiment_id", "override_path", "value", "control"}
_MODEL_CONFIG_KEYS = {
    "schema_version",
    "experiment_name",
    "mode",
    "seed",
    "title",
    "dssm",
    "deepfm",
    "evaluation",
}
_TITLE_KEYS = {
    "enabled",
    "bucket_count",
    "ngram_min",
    "ngram_max",
    "embedding_dim",
    "maximum_tokens",
}
_DSSM_KEYS = {
    "embedding_dim",
    "hidden_dims",
    "output_dim",
    "dropout",
    "temperature",
    "learning_rate",
    "batch_size",
    "epochs",
    "patience",
    "min_delta",
    "negative_sampling",
    "negatives_per_positive",
    "popularity_alpha",
    "candidate_top_n",
    "time_decay",
}
_DEEPFM_KEYS = {
    "embedding_dim",
    "hidden_dims",
    "dropout",
    "learning_rate",
    "batch_size",
    "epochs",
    "patience",
    "min_delta",
    "negatives_per_positive",
    "dense_features",
}
_EVALUATION_KEYS = {
    "candidate_policy",
    "k",
    "maximum_badcases",
    "activity_segments",
}
_ALLOWED_OVERRIDE_PATHS = {
    "dssm",
    "deepfm",
    "dssm.negative_sampling",
    "dssm.time_decay",
    "title.enabled",
}
_SELECTION_POLICY = "serial_validation_selection_then_single_test_final"
_TEST_POLICY = "selected_configuration_only_once"
_PERSONALIZED_RECALL_SOURCES = frozenset(
    {"dssm", "item_item_cf", "profile_title", "popular", "explore"}
)
_SOURCE_ABLATIONS = {
    "recall-all",
    "recall-without-dssm",
    "recall-without-cf",
    "recall-without-profile-title",
}
_ABLATION_CONTRACT = {
    "recall-all": ({"dssm", "item_item_cf", "profile_title"}, False, False),
    "recall-without-dssm": ({"item_item_cf", "profile_title"}, False, False),
    "recall-without-cf": ({"dssm", "profile_title"}, False, False),
    "recall-without-profile-title": ({"dssm", "item_item_cf"}, False, False),
    "topic-dedup-on": ({"dssm", "item_item_cf", "profile_title"}, True, False),
    "mmr-on": ({"dssm", "item_item_cf", "profile_title"}, False, True),
}
_GIT_SHA_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_IMAGE_PATTERN = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")
_NAMESPACE_MARKER = ".phase7a-namespace.json"


@dataclass(frozen=True, slots=True)
class ResolvedExperiment:
    experiment_id: str
    control: str
    override_path: str
    config: dict[str, Any]
    config_checksum: str


@dataclass(frozen=True, slots=True)
class ResolvedMatrix:
    matrix_path: Path
    matrix_checksum: str
    base_config_path: Path
    base_config_checksum: str
    selection_metric: str
    experiments: tuple[ResolvedExperiment, ...]
    serving_ablation: dict[str, Any]


def _strict_keys(value: object, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ModelInputError(f"{label} has unknown or missing fields")
    return value


def _safe_repo_file(repo_root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ModelInputError(f"{label} must be a repository-relative path")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise ModelInputError(f"{label} must stay within the repository")
    target = (repo_root / raw).resolve()
    if (
        not target.is_relative_to(repo_root.resolve())
        or target.is_symlink()
        or not target.is_file()
    ):
        raise ModelInputError(f"{label} is missing or unsafe")
    return target


def _apply_override(config: dict[str, Any], path: str, value: object) -> None:
    if path not in _ALLOWED_OVERRIDE_PATHS:
        raise ModelInputError(f"matrix override path is not allowed: {path}")
    parts = path.split(".")
    target: dict[str, Any] = config
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            raise ModelInputError(f"matrix override path does not resolve: {path}")
        target = child
    if parts[-1] not in target:
        raise ModelInputError(f"matrix override path does not exist: {path}")
    target[parts[-1]] = copy.deepcopy(value)


def _strict_model_config_shape(config: object) -> None:
    root = _strict_keys(config, _MODEL_CONFIG_KEYS, label="Phase 7A model config")
    _strict_keys(root["title"], _TITLE_KEYS, label="Phase 7A title config")
    dssm = _strict_keys(root["dssm"], _DSSM_KEYS, label="Phase 7A DSSM config")
    _strict_keys(dssm["time_decay"], {"enabled", "half_life_seconds"}, label="time decay")
    _strict_keys(root["deepfm"], _DEEPFM_KEYS, label="Phase 7A DeepFM config")
    _strict_keys(root["evaluation"], _EVALUATION_KEYS, label="Phase 7A evaluation config")


def _validate_matrix_coverage(experiments: Sequence[ResolvedExperiment]) -> None:
    by_path: dict[str, list[ResolvedExperiment]] = {}
    for row in experiments:
        by_path.setdefault(row.override_path, []).append(row)
    negative_values = {
        row.config["dssm"]["negative_sampling"] for row in by_path.get("dssm.negative_sampling", [])
    }
    if not {"uniform", "popularity_aware"} <= negative_values:
        raise ModelInputError("matrix must compare uniform and popularity-aware negatives")
    decay_values = {
        (
            row.config["dssm"]["time_decay"]["enabled"],
            row.config["dssm"]["time_decay"]["half_life_seconds"],
        )
        for row in by_path.get("dssm.time_decay", [])
    }
    if not {(False, 604800), (True, 259200), (True, 1209600)} <= decay_values:
        raise ModelInputError("matrix must contain decay off, 3-day and 14-day variants")
    title_values = {row.config["title"]["enabled"] for row in by_path.get("title.enabled", [])}
    if title_values != {False, True}:
        raise ModelInputError("matrix must compare title features off and on")
    if len(by_path.get("dssm", [])) < 2 or len(by_path.get("deepfm", [])) < 2:
        raise ModelInputError("matrix must contain two DSSM and two DeepFM hyperparameter groups")


def _validate_serving_ablation(value: object) -> dict[str, Any]:
    root = _strict_keys(value, {"cohort", "experiments"}, label="serving_ablation")
    cohort = _strict_keys(
        root["cohort"],
        {"split", "users", "candidate_pool_size", "candidate_policy", "k"},
        label="serving_ablation.cohort",
    )
    if (
        cohort["split"] != "test"
        or cohort["users"] != "all_split_users"
        or cohort["candidate_policy"] != "serving_top_n_excluding_train_seen"
    ):
        raise ModelInputError("serving ablation cohort must use the frozen full-test contract")
    pool_size = cohort["candidate_pool_size"]
    k_values = cohort["k"]
    if (
        isinstance(pool_size, bool)
        or not isinstance(pool_size, int)
        or pool_size < 1
        or not isinstance(k_values, list)
        or not k_values
        or any(isinstance(k, bool) or not isinstance(k, int) or k < 1 for k in k_values)
        or len(k_values) != len(set(k_values))
        or pool_size < max(k_values)
    ):
        raise ModelInputError("serving ablation candidate/K contract is invalid")
    rows = root["experiments"]
    if not isinstance(rows, list) or not rows:
        raise ModelInputError("serving ablation experiments must be non-empty")
    identifiers: set[str] = set()
    for row in rows:
        document = _strict_keys(
            row,
            {"experiment_id", "enabled_sources", "topic_dedup_enabled", "mmr_enabled"},
            label="serving ablation experiment",
        )
        experiment_id = document["experiment_id"]
        sources = document["enabled_sources"]
        if not isinstance(experiment_id, str) or not experiment_id or experiment_id in identifiers:
            raise ModelInputError("serving ablation experiment IDs must be unique strings")
        identifiers.add(experiment_id)
        if (
            not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) for source in sources)
            or len(sources) != len(set(sources))
            or not set(sources) <= _PERSONALIZED_RECALL_SOURCES
        ):
            raise ModelInputError("serving ablation enabled_sources are invalid")
        if not isinstance(document["topic_dedup_enabled"], bool) or not isinstance(
            document["mmr_enabled"], bool
        ):
            raise ModelInputError("serving ablation diversity flags must be boolean")
    if not _SOURCE_ABLATIONS <= identifiers or not {"topic-dedup-on", "mmr-on"} <= identifiers:
        raise ModelInputError("serving ablation matrix is incomplete")
    by_id = {row["experiment_id"]: row for row in rows}
    for experiment_id, (sources, topic_enabled, mmr_enabled) in _ABLATION_CONTRACT.items():
        row = by_id[experiment_id]
        if (
            set(row["enabled_sources"]) != sources
            or row["topic_dedup_enabled"] is not topic_enabled
            or row["mmr_enabled"] is not mmr_enabled
        ):
            raise ModelInputError(f"serving ablation contract drifted for {experiment_id}")
    return copy.deepcopy(root)


def resolve_matrix(matrix_path: str | Path, *, repo_root: str | Path = ".") -> ResolvedMatrix:
    repository = Path(repo_root).resolve()
    matrix = _safe_repo_file(repository, str(matrix_path), label="matrix path")
    try:
        document = json.loads(matrix.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelInputError("experiment matrix is not valid JSON") from exc
    root = _strict_keys(document, _MATRIX_KEYS, label="experiment matrix")
    if root["schema_version"] != "2.0" or root["phase"] != "7A":
        raise ModelInputError("experiment matrix schema/phase is unsupported")
    if root["execution_policy"] != _SELECTION_POLICY:
        raise ModelInputError("experiment matrix execution policy is unsupported")
    selection = _strict_keys(
        root["selection"],
        {"metric", "direction", "tie_break", "test_policy"},
        label="matrix selection",
    )
    if (
        selection["direction"] != "maximize"
        or selection["tie_break"] != "experiment_id_ascending"
        or selection["test_policy"] != _TEST_POLICY
        or selection["metric"] not in {"dssm.ndcg@20", "two_stage.ndcg@20"}
    ):
        raise ModelInputError("matrix selection contract is unsupported")
    base_path = _safe_repo_file(repository, root["base_config"], label="base_config")
    base_config, base_checksum = load_model_config(base_path)
    _strict_model_config_shape(base_config)
    if base_config["mode"] != "full":
        raise ModelInputError("Phase 7A base config must use full mode")
    rows = root["model_experiments"]
    if not isinstance(rows, list) or not rows:
        raise ModelInputError("model_experiments must be non-empty")
    identifiers: set[str] = set()
    resolved: list[ResolvedExperiment] = []
    for raw in rows:
        row = _strict_keys(raw, _MODEL_EXPERIMENT_KEYS, label="model experiment")
        experiment_id = row["experiment_id"]
        control = row["control"]
        override_path = row["override_path"]
        if (
            not isinstance(experiment_id, str)
            or not experiment_id
            or experiment_id in identifiers
            or not isinstance(control, str)
            or not control
            or not isinstance(override_path, str)
        ):
            raise ModelInputError("model experiment identity fields are invalid")
        validate_relative_file_name(experiment_id)
        identifiers.add(experiment_id)
        config = copy.deepcopy(base_config)
        _apply_override(config, override_path, row["value"])
        config, checksum = load_model_config(config)
        _strict_model_config_shape(config)
        resolved.append(ResolvedExperiment(experiment_id, control, override_path, config, checksum))
    if any(row.control not in identifiers for row in resolved):
        raise ModelInputError("every matrix control must reference an experiment ID")
    _validate_matrix_coverage(resolved)
    ablation = _validate_serving_ablation(root["serving_ablation"])
    return ResolvedMatrix(
        matrix_path=matrix,
        matrix_checksum=sha256_file(matrix),
        base_config_path=base_path,
        base_config_checksum=base_checksum,
        selection_metric=selection["metric"],
        experiments=tuple(resolved),
        serving_ablation=ablation,
    )


def resolved_matrix_document(matrix: ResolvedMatrix) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "matrix_path": str(matrix.matrix_path),
        "matrix_checksum": matrix.matrix_checksum,
        "base_config_path": str(matrix.base_config_path),
        "base_config_checksum": matrix.base_config_checksum,
        "selection_metric": matrix.selection_metric,
        "experiments": [
            {
                "experiment_id": row.experiment_id,
                "control": row.control,
                "override_path": row.override_path,
                "config_checksum": row.config_checksum,
                "resolved_config": row.config,
            }
            for row in matrix.experiments
        ],
        "serving_ablation": matrix.serving_ablation,
    }


def _metric_value(metrics: Mapping[str, Any], metric_path: str) -> float:
    group, name = metric_path.split(".", maxsplit=1)
    try:
        value = metrics[group][name]
    except (KeyError, TypeError) as exc:
        raise ModelInputError(f"selection metric is missing: {metric_path}") from exc
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelInputError(f"selection metric is not numeric: {metric_path}")
    return float(value)


def _stage_record(trained: TrainedModelStages) -> dict[str, Any]:
    def stage(row: Any) -> dict[str, Any]:
        return {
            "best_epoch": row.best_epoch,
            "best_validation_metric": row.best_validation_metric,
            "stop_reason": row.stop_reason,
            "history": list(row.history),
            "resumed_from_epoch": row.resumed_from_epoch,
        }

    return {"dssm": stage(trained.dssm_stage), "deepfm": stage(trained.deepfm_stage)}


def _empty_segment_accumulators(k_values: Sequence[int]) -> dict[str, RankingMetricAccumulator]:
    return {name: RankingMetricAccumulator(k_values) for name in ACTIVITY_SEGMENTS}


def _accumulator_document(
    overall: RankingMetricAccumulator,
    segments: Mapping[str, RankingMetricAccumulator],
    k_values: Sequence[int],
) -> dict[str, Any]:
    empty = {
        metric: 0.0
        for k in sorted(set(k_values))
        for metric in (f"recall@{k}", f"ndcg@{k}", f"hit_rate@{k}")
    }
    return {
        "overall": overall.result(),
        "segments": {
            name: {
                "user_count": accumulator.user_count,
                **(accumulator.result() if accumulator.user_count else empty),
            }
            for name, accumulator in segments.items()
        },
    }


def evaluate_serving_ablations(
    trained: TrainedModelStages,
    *,
    bundle_path: Path,
    manifest_checksum: str,
    matrix: ResolvedMatrix,
) -> dict[str, Any]:
    """Evaluate serving primitives against an immutable, DB-free full-test cohort."""

    from apps.api.app.feeds.ranking import merge_recall, mmr_rank, topic_deduplicate
    from apps.api.app.feeds.retrieval import (
        CatalogItem,
        ItemItemIndex,
        build_profile_title_preferences,
        rank_candidates,
        retrieve_candidates,
    )
    from recsys.models.baselines import relevant_by_user
    from recsys.models.bundle import load_bundle
    from recsys.serving.runtime import LoadedRecommendationModel

    data = trained.data
    if not data.test_loaded:
        raise ModelInputError("serving ablations require the explicitly loaded test cohort")
    ablation = matrix.serving_ablation
    cohort = ablation["cohort"]
    k_values = [int(value) for value in cohort["k"]]
    pool_size = int(cohort["candidate_pool_size"])
    relevant = relevant_by_user(data.test)
    users = tuple(sorted(relevant))
    bundle = load_bundle(bundle_path, manifest_checksum)
    loaded = LoadedRecommendationModel(bundle)
    catalog_rows = tuple(
        CatalogItem(item_id=item_id, title=data.titles[item_id], cover=None, likes=0, views=0)
        for item_id in data.item_ids
    )
    catalog = {row.item_id: row for row in catalog_rows}
    item_item_index = ItemItemIndex.from_histories(data.user_train_items)
    accumulators = {
        row["experiment_id"]: (
            RankingMetricAccumulator(k_values),
            _empty_segment_accumulators(k_values),
        )
        for row in ablation["experiments"]
    }
    source_contract = {
        "dssm": "selected_bundle_top_n",
        "item_item_cf": "train_history_cosine_seeded_by_full_user_train_history",
        "profile_title": "train_history_normalized_title_token_frequency",
        "per_source_top_n": pool_size,
        "train_seen_excluded_after_retrieval": True,
        "topic_max_per_group": 1,
        "mmr_lambda": 0.75,
    }

    for user_id in users:
        history = data.user_train_items[user_id]
        history_titles = [data.titles[item_id] for item_id in history]
        preferences = build_profile_title_preferences(history_titles)
        dssm_rows = loaded.recall(user_id, top_n=pool_size)

        def recalled(
            _user_id: str,
            top_n: int,
            *,
            expected_user_id: str = user_id,
            rows: Sequence[tuple[str, float]] = tuple(dssm_rows),
        ) -> Sequence[tuple[str, float]]:
            if _user_id != expected_user_id:
                raise ValueError("ablation DSSM cache crossed its user boundary")
            return rows[:top_n]

        control_retrieval = retrieve_candidates(
            feed_type="personalized",
            catalog=catalog_rows,
            bundle=bundle,
            source_user_id=user_id,
            profile_title_preferences=preferences,
            recent_item_ids=history,
            item_item_index=item_item_index,
            seed=int(trained.config["seed"]),
            top_n=pool_size,
            enabled_sources={"dssm", "item_item_cf", "profile_title"},
            dssm_recaller=recalled,
        )
        seen = set(history)
        for row in ablation["experiments"]:
            enabled_sources = set(row["enabled_sources"])
            recall = [
                candidate
                for candidate in control_retrieval.candidates
                if candidate.source in enabled_sources and candidate.item_id not in seen
            ]
            ranking = rank_candidates(
                merged=merge_recall(recall),
                catalog=catalog,
                bundle=bundle,
                source_user_id=user_id,
                positive_history_titles=history_titles,
                profile_activity_count=len(history),
            )
            ranked = list(ranking.candidates)
            if row["topic_dedup_enabled"]:
                ranked, _removed = topic_deduplicate(ranked, max_per_topic=1)
            if row["mmr_enabled"]:
                ranked, _steps = mmr_rank(ranked, vectors=ranking.title_vectors, lambda_value=0.75)
            item_ids = [candidate.item_id for candidate in ranked[: max(k_values)]]
            overall, segments = accumulators[row["experiment_id"]]
            overall.add(item_ids, relevant[user_id])
            segments[activity_segment(len(history))].add(item_ids, relevant[user_id])

    cohort_identity = {
        "data_version": data.data_version,
        "data_manifest_checksum": data.manifest_checksum,
        "split": cohort["split"],
        "users": list(users),
        "candidate_pool_size": pool_size,
        "candidate_policy": cohort["candidate_policy"],
        "k": k_values,
        "source_contract": source_contract,
    }
    return {
        "cohort": {
            **cohort,
            "user_count": len(users),
            "cohort_checksum": sha256_bytes(canonical_json_bytes(cohort_identity)),
            "source_contract": source_contract,
        },
        "activity_segment_definitions": ACTIVITY_SEGMENTS,
        "experiments": {
            row["experiment_id"]: {
                "enabled_sources": row["enabled_sources"],
                "topic_dedup_enabled": row["topic_dedup_enabled"],
                "mmr_enabled": row["mmr_enabled"],
                **_accumulator_document(*accumulators[row["experiment_id"]], k_values),
            }
            for row in ablation["experiments"]
        },
    }


def _environment(path: Path) -> dict[str, Any]:
    import torch

    disk = shutil.disk_usage(path)
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else None
    pages = os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "torch": torch.__version__,
        "cpu_count": os.cpu_count(),
        "physical_memory_bytes": page_size * pages if page_size and pages else None,
        "disk_total_bytes": disk.total,
        "disk_free_bytes_at_start": disk.free,
    }


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
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


def _verify_runtime_identity(
    *,
    matrix: ResolvedMatrix,
    repo_root: str | Path,
    git_revision: str,
    image_digest: str,
    requested_source_checksum: str,
    attestation_path: str | Path,
) -> dict[str, Any]:
    if not _GIT_SHA_PATTERN.fullmatch(git_revision):
        raise ModelInputError("git_revision must be an explicit lowercase 40-character SHA")
    if not _IMAGE_PATTERN.fullmatch(image_digest):
        raise ModelInputError("image_digest must be an exact name@sha256:<64> reference")
    if not SHA256_PATTERN.fullmatch(requested_source_checksum):
        raise ModelInputError("source_checksum must be lowercase SHA-256")
    attestation = load_attestation(attestation_path)
    recomputed_source_checksum = source_checksum(repo_root)
    if attestation["git_revision"] != git_revision:
        raise ModelInputError("requested Git revision does not match the baked attestation")
    if attestation["source_checksum"] != requested_source_checksum:
        raise ModelInputError("requested source checksum does not match the baked attestation")
    if recomputed_source_checksum != requested_source_checksum:
        raise ModelInputError("requested source checksum does not match the container source")
    environment_revision = os.environ.get("GIT_REVISION")
    if environment_revision is not None and environment_revision != git_revision:
        raise ModelInputError("GIT_REVISION environment does not match the requested revision")
    return {
        "git_revision": git_revision,
        "image_reference": image_digest,
        "image_digest": image_digest.rsplit("@", 1)[1],
        "source_checksum": requested_source_checksum,
        "baked_git_revision": attestation["git_revision"],
        "baked_source_checksum": attestation["source_checksum"],
        "recomputed_source_checksum": recomputed_source_checksum,
        "matrix_checksum": matrix.matrix_checksum,
        "base_config_checksum": matrix.base_config_checksum,
    }


def _namespace_identity(
    *,
    runtime_identity: Mapping[str, Any],
    data_version: str,
    data_manifest_checksum: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "git_revision": runtime_identity["git_revision"],
        "image_reference": runtime_identity["image_reference"],
        "image_digest": runtime_identity["image_digest"],
        "source_checksum": runtime_identity["source_checksum"],
        "matrix_checksum": runtime_identity["matrix_checksum"],
        "base_config_checksum": runtime_identity["base_config_checksum"],
        "data_version": data_version,
        "data_manifest_checksum": data_manifest_checksum,
    }


def _ensure_namespace(output_root: str | Path, identity: Mapping[str, Any]) -> Path:
    root = Path(output_root)
    if root.is_symlink():
        raise ModelInputError("Phase 7A output namespace must not be a symlink")
    if not root.exists():
        root.mkdir(parents=True)
    if not root.is_dir():
        raise ModelInputError("Phase 7A output namespace must be a directory")
    marker = root / _NAMESPACE_MARKER
    entries = tuple(root.iterdir())
    if marker in entries:
        raise ModelInputError("Phase 7A namespace is already claimed")
    if entries:
        raise ModelInputError("non-empty unmarked Phase 7A namespace is refused")
    payload = canonical_json_bytes(identity) + b"\n"
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ModelInputError("Phase 7A namespace is already claimed") from exc
    try:
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("Phase 7A namespace marker write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(root)
    except Exception:
        marker.unlink(missing_ok=True)
        try:
            fsync_directory(root)
        except OSError:
            pass
        raise
    return root


def _decode_mount_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mount_table() -> dict[str, dict[str, Any]]:
    mounts: dict[str, dict[str, Any]] = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        trailing = right.split()
        mount_point = _decode_mount_path(fields[4])
        mounts[mount_point] = {
            "mount_options": sorted(fields[5].split(",")),
            "optional_fields": fields[6:],
            "filesystem": trailing[0],
            "source": trailing[1],
            "super_options": sorted(trailing[2].split(",")),
        }
    return mounts


def _read_cgroup_value(name: str) -> str:
    path = Path("/sys/fs/cgroup") / name
    if not path.is_file():
        raise ModelInputError("Phase 7A requires a cgroup v2 runtime")
    return path.read_text().strip()


def _has_default_route() -> bool:
    ipv4 = Path("/proc/net/route")
    if ipv4.is_file():
        for line in ipv4.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[0] != "lo" and fields[1] == "00000000":
                return True
    ipv6 = Path("/proc/net/ipv6_route")
    if ipv6.is_file():
        for line in ipv6.read_text().splitlines():
            fields = line.split()
            if (
                len(fields) >= 2
                and fields[-1] != "lo"
                and fields[0] == "0" * 32
                and fields[1] == "00"
            ):
                return True
    return False


def _runtime_envelope() -> dict[str, Any]:
    mounts = _mount_table()
    try:
        root_mount = mounts["/"]
        temporary_mount = mounts["/tmp"]
        processed_mount = mounts["/artifacts/processed"]
        output_mount = mounts["/phase7a"]
    except KeyError as exc:
        raise ModelInputError(f"required Phase 7A mount is absent: {exc.args[0]}") from exc
    temporary_options = set(temporary_mount["mount_options"]) | set(
        temporary_mount["super_options"]
    )
    required_temporary_options = {"rw", "noexec", "nosuid", "nodev"}
    size_is_256m = bool({"size=256m", "size=262144k", "size=268435456"} & temporary_options)
    memory_max = _read_cgroup_value("memory.max")
    memory_swap_max = _read_cgroup_value("memory.swap.max")
    cpu_max = _read_cgroup_value("cpu.max")
    pids_max = _read_cgroup_value("pids.max")
    try:
        cpu_quota, cpu_period = (int(value) for value in cpu_max.split())
    except (TypeError, ValueError) as exc:
        raise ModelInputError("Phase 7A CPU quota is not numeric") from exc
    kernel_interfaces = sorted(name for _index, name in socket.if_nameindex())
    interface_states = {
        name: (Path("/sys/class/net") / name / "operstate").read_text().strip()
        for name in kernel_interfaces
    }
    interfaces = sorted(
        name for name, state in interface_states.items() if name == "lo" or state != "down"
    )
    inactive_kernel_interfaces = sorted(set(kernel_interfaces) - set(interfaces))
    default_route = _has_default_route()
    relevant_environment = sorted(
        name
        for name in os.environ
        if name.startswith("COMPOSE_") or name in {"DOCKER_HOST", "ENV_FILE"}
    )
    unexpected_writable_bind_mounts = sorted(
        mount_point
        for mount_point, details in mounts.items()
        if mount_point not in {"/artifacts/processed", "/phase7a"}
        and "rw" in details["mount_options"]
        and details["filesystem"] == output_mount["filesystem"]
        and mount_point not in {"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}
    )
    failures = []
    if "ro" not in root_mount["mount_options"]:
        failures.append("root filesystem is writable")
    if (
        temporary_mount["filesystem"] != "tmpfs"
        or not required_temporary_options <= temporary_options
    ):
        failures.append("/tmp is not the required restricted tmpfs")
    if not size_is_256m:
        failures.append("/tmp is not limited to 256 MiB")
    if "ro" not in processed_mount["mount_options"]:
        failures.append("processed input bind is writable")
    if "rw" not in output_mount["mount_options"]:
        failures.append("Phase 7A output bind is not writable")
    if unexpected_writable_bind_mounts:
        failures.append("unexpected writable bind mounts are visible")
    if memory_max != str(5 * 1024**3) or memory_swap_max != "0":
        failures.append("memory or swap cgroup limit drifted")
    if cpu_quota / cpu_period != 4.0:
        failures.append("CPU cgroup limit drifted")
    if pids_max != "512":
        failures.append("PID cgroup limit drifted")
    if interfaces != ["lo"] or default_route:
        failures.append("network namespace is not loopback-only")
    if relevant_environment:
        failures.append("Compose or Docker host environment leaked into the container")
    if Path("/workspace/.env").exists() or Path("/var/run/docker.sock").exists():
        failures.append("forbidden environment or Docker socket is visible")
    if any(Path("/phase7a").iterdir()):
        failures.append("Phase 7A output bind is not empty before execution")
    if failures:
        raise ModelInputError("Phase 7A runtime envelope mismatch: " + "; ".join(failures))
    return {
        "root_filesystem_read_only": True,
        "tmp": {
            "filesystem": temporary_mount["filesystem"],
            "required_options": sorted(required_temporary_options),
            "size_bytes": 256 * 1024**2,
        },
        "cgroup": {
            "memory_max_bytes": int(memory_max),
            "memory_swap_max_bytes": int(memory_swap_max),
            "cpu_quota": cpu_quota,
            "cpu_period": cpu_period,
            "cpu_limit": cpu_quota / cpu_period,
            "pids_max": int(pids_max),
        },
        "network": {
            "interfaces": interfaces,
            "inactive_kernel_interfaces": inactive_kernel_interfaces,
            "default_route": default_route,
        },
        "bind_mounts": {
            "processed": {"path": "/artifacts/processed", "read_only": True},
            "output": {"path": "/phase7a", "read_only": False},
            "unexpected_writable": unexpected_writable_bind_mounts,
        },
        "forbidden_visibility": {
            "compose_environment": relevant_environment,
            "workspace_dotenv": False,
            "docker_socket": False,
            "review_service_route": False,
        },
    }


def preflight_phase7a(
    *,
    matrix_path: str | Path,
    repo_root: str | Path,
    processed_root: str | Path,
    data_version: str,
    data_manifest_checksum: str,
    output_root: str | Path,
    run_id: str,
    git_revision: str,
    image_digest: str,
    requested_source_checksum: str,
    attestation_path: str | Path,
) -> dict[str, Any]:
    """Validate formal identity and runtime constraints without reading data or writing output."""

    validate_relative_file_name(data_version)
    validate_relative_file_name(run_id)
    if data_version.lower() == "latest":
        raise ModelInputError("data_version must never be latest")
    if not SHA256_PATTERN.fullmatch(data_manifest_checksum):
        raise ModelInputError("data_manifest_checksum must be lowercase SHA-256")
    if Path(processed_root) != Path("/artifacts/processed") or Path(output_root) != Path(
        "/phase7a"
    ):
        raise ModelInputError("Phase 7A preflight mount destinations drifted")
    matrix = resolve_matrix(matrix_path, repo_root=repo_root)
    identity = _verify_runtime_identity(
        matrix=matrix,
        repo_root=repo_root,
        git_revision=git_revision,
        image_digest=image_digest,
        requested_source_checksum=requested_source_checksum,
        attestation_path=attestation_path,
    )
    return {
        "schema_version": "1.0",
        "status": "PASS",
        "mode": "preflight-no-data",
        "identity": identity,
        "runtime_envelope": _runtime_envelope(),
        "data_read": False,
        "output_written": False,
    }


def _run_phase7a(
    *,
    matrix_path: str | Path,
    repo_root: str | Path,
    processed_root: str | Path,
    data_version: str,
    data_manifest_checksum: str,
    output_root: str | Path,
    run_id: str,
    git_revision: str,
    image_digest: str,
    requested_source_checksum: str,
    attestation_path: str | Path,
    command: Sequence[str],
    codec: Any = None,
) -> Path:
    from recsys.models.data import validate_data_manifest_identity
    from recsys.models.entrypoint import (
        evaluate_validation_selection,
        finalize_trained_model,
        load_trained_model_test_split,
        train_model_stages,
    )

    if not SHA256_PATTERN.fullmatch(data_manifest_checksum):
        raise ModelInputError("data_manifest_checksum must be lowercase SHA-256")
    validate_relative_file_name(run_id)
    matrix = resolve_matrix(matrix_path, repo_root=repo_root)
    runtime_identity = _verify_runtime_identity(
        matrix=matrix,
        repo_root=repo_root,
        git_revision=git_revision,
        image_digest=image_digest,
        requested_source_checksum=requested_source_checksum,
        attestation_path=attestation_path,
    )
    validate_data_manifest_identity(
        processed_root=processed_root,
        data_version=data_version,
        data_manifest_checksum=data_manifest_checksum,
    )
    runtime_envelope = _runtime_envelope()
    namespace = _ensure_namespace(
        output_root,
        _namespace_identity(
            runtime_identity=runtime_identity,
            data_version=data_version,
            data_manifest_checksum=data_manifest_checksum,
        ),
    )
    run_path = namespace / run_id
    run_path.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "RUNNING",
        "run_id": run_id,
        "git_revision": git_revision,
        "image_reference": runtime_identity["image_reference"],
        "image_digest": runtime_identity["image_digest"],
        "source_checksum": runtime_identity["source_checksum"],
        "runtime_identity": runtime_identity,
        "runtime_envelope": runtime_envelope,
        "data_version": data_version,
        "data_manifest_checksum": data_manifest_checksum,
        "matrix_checksum": matrix.matrix_checksum,
        "base_config_checksum": matrix.base_config_checksum,
        "selection_policy": _SELECTION_POLICY,
        "selection_metric": matrix.selection_metric,
        "test_policy": _TEST_POLICY,
        "command": list(command),
        "environment": None,
        "validation_runs": [],
    }
    result_path = run_path / "run.json"
    _write_json_atomic(result_path, record)
    record["environment"] = _environment(run_path)
    _write_json_atomic(result_path, record)
    (run_path / "resolved-configs").mkdir()
    for experiment in matrix.experiments:
        _write_json_atomic(
            run_path / "resolved-configs" / f"{experiment.experiment_id}.json",
            experiment.config,
        )
    selected: ResolvedExperiment | None = None
    selected_metric = float("-inf")
    execution_cache: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for experiment in matrix.experiments:
        run_started = time.monotonic()
        cached = execution_cache.get(experiment.config_checksum)
        if cached is None:
            trained = train_model_stages(
                processed_root=processed_root,
                data_version=data_version,
                data_manifest_checksum=data_manifest_checksum,
                config=experiment.config,
                output_root=run_path / "models",
                checkpoint_root=run_path / "checkpoints" / experiment.experiment_id,
                codec=codec,
            )
            metrics = evaluate_validation_selection(trained)
            stages = _stage_record(trained)
            execution_cache[experiment.config_checksum] = (
                experiment.experiment_id,
                copy.deepcopy(metrics),
                copy.deepcopy(stages),
            )
            execution_reused = False
            reused_execution_from = None
        else:
            reused_execution_from, cached_metrics, cached_stages = cached
            metrics = copy.deepcopy(cached_metrics)
            stages = copy.deepcopy(cached_stages)
            execution_reused = True
        metric = _metric_value(metrics, matrix.selection_metric)
        record["validation_runs"].append(
            {
                "experiment_id": experiment.experiment_id,
                "control": experiment.control,
                "override_path": experiment.override_path,
                "config_checksum": experiment.config_checksum,
                "seed": int(experiment.config["seed"]),
                "split": "validation",
                "metrics": metrics,
                "stages": stages,
                "execution_reused": execution_reused,
                "reused_execution_from": reused_execution_from,
                "elapsed_seconds": time.monotonic() - run_started,
            }
        )
        if metric > selected_metric or (
            metric == selected_metric
            and (selected is None or experiment.experiment_id < selected.experiment_id)
        ):
            selected = experiment
            selected_metric = metric
        _write_json_atomic(result_path, record)
    if selected is None:
        raise ModelInputError("matrix produced no selected experiment")
    record["selection"] = {
        "experiment_id": selected.experiment_id,
        "config_checksum": selected.config_checksum,
        "metric": matrix.selection_metric,
        "value": selected_metric,
        "frozen_before_test": True,
    }
    _write_json_atomic(result_path, record)

    final_trained = train_model_stages(
        processed_root=processed_root,
        data_version=data_version,
        data_manifest_checksum=data_manifest_checksum,
        config=selected.config,
        output_root=run_path / "models",
        checkpoint_root=run_path / "final-checkpoints",
        codec=codec,
    )
    if final_trained.config_checksum != selected.config_checksum:
        raise ModelInputError("final training config drifted after validation selection")
    selected_stage_record = next(
        row["stages"]
        for row in record["validation_runs"]
        if row["experiment_id"] == selected.experiment_id
    )
    if _stage_record(final_trained) != selected_stage_record:
        raise ModelInputError("deterministic final retraining diverged from selection history")
    final_trained = load_trained_model_test_split(
        final_trained,
        processed_root=processed_root,
        codec=codec,
    )
    artifact = finalize_trained_model(
        final_trained, output_root=run_path / "models", git_revision=git_revision
    )
    if (
        artifact.manifest.get("git_revision") != git_revision
        or artifact.manifest.get("data_version") != data_version
        or artifact.manifest.get("data_manifest_checksum") != data_manifest_checksum
        or artifact.manifest.get("resolved_config_checksum") != selected.config_checksum
    ):
        raise ModelInputError("final artifact identity does not match the frozen selection")
    ablations = evaluate_serving_ablations(
        final_trained,
        bundle_path=artifact.bundle_path,
        manifest_checksum=artifact.manifest_checksum,
        matrix=matrix,
    )
    ablation_path = run_path / "serving-ablations.json"
    _write_json_atomic(ablation_path, ablations)
    record.update(
        {
            "status": "PASS",
            "elapsed_seconds": time.monotonic() - started,
            "final_test": {
                "split": "test",
                "test_evaluation_count": 1,
                "model_version": artifact.model_version,
                "manifest_checksum": artifact.manifest_checksum,
                "bundle_checksum": sha256_file(artifact.bundle_path),
                "config_checksum": selected.config_checksum,
                "stages": _stage_record(final_trained),
                "metrics": json.loads((artifact.path / "metrics.json").read_bytes()),
            },
            "serving_ablation_artifact": {
                "path": ablation_path.name,
                "sha256": sha256_file(ablation_path),
            },
        }
    )
    _write_json_atomic(result_path, record)
    return result_path


def run_phase7a(
    *,
    matrix_path: str | Path,
    repo_root: str | Path,
    processed_root: str | Path,
    data_version: str,
    data_manifest_checksum: str,
    output_root: str | Path,
    run_id: str,
    git_revision: str,
    image_digest: str,
    requested_source_checksum: str,
    attestation_path: str | Path,
    command: Sequence[str],
    codec: Any = None,
) -> Path:
    """Run Phase 7A and persist a terminal failure record after execution starts."""

    started = time.monotonic()
    try:
        validate_relative_file_name(run_id)
        run_path_existed = (Path(output_root) / run_id).exists()
    except ValueError:
        run_path_existed = True
    try:
        return _run_phase7a(
            matrix_path=matrix_path,
            repo_root=repo_root,
            processed_root=processed_root,
            data_version=data_version,
            data_manifest_checksum=data_manifest_checksum,
            output_root=output_root,
            run_id=run_id,
            git_revision=git_revision,
            image_digest=image_digest,
            requested_source_checksum=requested_source_checksum,
            attestation_path=attestation_path,
            command=command,
            codec=codec,
        )
    except Exception as exc:
        result_path = Path(output_root) / run_id / "run.json"
        if not run_path_existed and result_path.is_file() and not result_path.is_symlink():
            try:
                record = json.loads(result_path.read_bytes())
            except (UnicodeDecodeError, json.JSONDecodeError):
                record = {"schema_version": "1.0", "run_id": run_id, "validation_runs": []}
            validation_runs = record.get("validation_runs")
            if not isinstance(validation_runs, list):
                validation_runs = []
                record["validation_runs"] = validation_runs
            message = str(exc).replace("\r", " ").replace("\n", " ")[:500]
            record.update(
                {
                    "status": "FAILED",
                    "failure_type": type(exc).__name__,
                    "failure_message": message,
                    "elapsed_seconds": time.monotonic() - started,
                    "completed_validation_runs": len(validation_runs),
                }
            )
            _write_json_atomic(result_path, record)
        raise
