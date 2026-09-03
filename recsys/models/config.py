from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from recsys.data.common import canonical_json_bytes, load_json_object, sha256_bytes

from .errors import ModelInputError
from .metrics import ACTIVITY_SEGMENTS

_DENSE_FEATURES = {
    "dssm_recall_score",
    "title_history_similarity",
    "train_popularity_log_normalized",
    "train_novelty",
    "train_user_activity_log_normalized",
    "train_time_decay_weight",
}


def load_model_config(value: Mapping[str, Any] | str | Path) -> tuple[dict[str, Any], str]:
    config = load_json_object(value)
    if config.get("schema_version") != "1.0":
        raise ModelInputError("model config schema_version must be 1.0")
    if not isinstance(config.get("experiment_name"), str) or not config["experiment_name"]:
        raise ModelInputError("model config requires experiment_name")
    if config.get("mode") not in {"smoke", "experiment", "full", "systems"}:
        raise ModelInputError("model config mode is unsupported")
    seed = config.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ModelInputError("model config seed must be a non-negative integer")
    title = config.get("title")
    dssm = config.get("dssm")
    deepfm = config.get("deepfm")
    evaluation = config.get("evaluation")
    if not all(isinstance(row, dict) for row in (title, dssm, deepfm, evaluation)):
        raise ModelInputError("model config requires title/dssm/deepfm/evaluation objects")
    if not isinstance(title.get("enabled"), bool):
        raise ModelInputError("title.enabled must be boolean")
    for field, minimum in (("bucket_count", 8), ("embedding_dim", 1), ("maximum_tokens", 1)):
        raw = title.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
            raise ModelInputError(f"title.{field} must be an integer >= {minimum}")
    ngram_min = title.get("ngram_min", 1)
    ngram_max = title.get("ngram_max", 2)
    if not all(
        isinstance(value, int) and not isinstance(value, bool) for value in (ngram_min, ngram_max)
    ):
        raise ModelInputError("title ngram bounds must be integers")
    if not 1 <= ngram_min <= ngram_max <= 4:
        raise ModelInputError("title ngram bounds must satisfy 1 <= min <= max <= 4")
    if dssm.get("negative_sampling") not in {
        "uniform",
        "popularity_aware",
        "train_only_hard",
    }:
        raise ModelInputError("unsupported DSSM negative sampling strategy")
    if not isinstance(evaluation.get("k"), list) or not evaluation["k"]:
        raise ModelInputError("evaluation.k must be a non-empty list")
    if any(isinstance(k, bool) or not isinstance(k, int) or k < 1 for k in evaluation["k"]):
        raise ModelInputError("evaluation.k must contain positive integers")
    if len(evaluation["k"]) != len(set(evaluation["k"])):
        raise ModelInputError("evaluation.k must not contain duplicates")
    if evaluation.get("candidate_policy") != "full_catalog_excluding_train_seen":
        raise ModelInputError("unsupported final evaluation candidate policy")
    maximum_badcases = evaluation.get("maximum_badcases", 200)
    if (
        isinstance(maximum_badcases, bool)
        or not isinstance(maximum_badcases, int)
        or maximum_badcases < 1
    ):
        raise ModelInputError("evaluation.maximum_badcases must be a positive integer")
    activity_segments = evaluation.get("activity_segments")
    if activity_segments is not None and activity_segments != ACTIVITY_SEGMENTS:
        raise ModelInputError(
            "evaluation.activity_segments must match the frozen segment boundaries"
        )
    for stage_name, stage in (("dssm", dssm), ("deepfm", deepfm)):
        for field in ("epochs", "patience", "batch_size"):
            raw = stage.get(field)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ModelInputError(f"{stage_name}.{field} must be positive")
        learning_rate = stage.get("learning_rate")
        if (
            isinstance(learning_rate, bool)
            or not isinstance(learning_rate, int | float)
            or not math.isfinite(float(learning_rate))
            or learning_rate <= 0
        ):
            raise ModelInputError(f"{stage_name}.learning_rate must be positive")
        dropout = stage.get("dropout", 0.0)
        if (
            isinstance(dropout, bool)
            or not isinstance(dropout, int | float)
            or not math.isfinite(float(dropout))
            or not 0 <= dropout < 1
        ):
            raise ModelInputError(f"{stage_name}.dropout must be in [0,1)")
        min_delta = stage.get("min_delta", 0.0)
        if (
            isinstance(min_delta, bool)
            or not isinstance(min_delta, int | float)
            or not math.isfinite(float(min_delta))
            or min_delta < 0
        ):
            raise ModelInputError(f"{stage_name}.min_delta must be finite and non-negative")
        for field in ("embedding_dim",):
            raw = stage.get(field)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ModelInputError(f"{stage_name}.{field} must be positive")
        hidden = stage.get("hidden_dims", [])
        if not isinstance(hidden, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in hidden
        ):
            raise ModelInputError(f"{stage_name}.hidden_dims must contain positive integers")
    for field in ("output_dim", "negatives_per_positive", "candidate_top_n"):
        raw = dssm.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ModelInputError(f"dssm.{field} must be positive")
    if int(dssm["candidate_top_n"]) < max(int(value) for value in evaluation["k"]):
        raise ModelInputError("dssm.candidate_top_n must cover the largest evaluation K")
    deepfm_negatives = deepfm.get("negatives_per_positive")
    if (
        isinstance(deepfm_negatives, bool)
        or not isinstance(deepfm_negatives, int)
        or deepfm_negatives < 1
    ):
        raise ModelInputError("deepfm.negatives_per_positive must be positive")
    temperature = dssm.get("temperature", 0.1)
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not math.isfinite(float(temperature))
        or temperature <= 0
    ):
        raise ModelInputError("dssm.temperature must be finite and positive")
    popularity_alpha = dssm.get("popularity_alpha", 0.75)
    if (
        isinstance(popularity_alpha, bool)
        or not isinstance(popularity_alpha, int | float)
        or not math.isfinite(float(popularity_alpha))
        or popularity_alpha < 0
    ):
        raise ModelInputError("dssm.popularity_alpha must be finite and non-negative")
    dense_features = deepfm.get("dense_features")
    if (
        not isinstance(dense_features, list)
        or len(dense_features) != len(_DENSE_FEATURES)
        or set(dense_features) != _DENSE_FEATURES
    ):
        raise ModelInputError("deepfm.dense_features must declare the frozen six feature names")
    decay = dssm.get("time_decay")
    if not isinstance(decay, dict) or not isinstance(decay.get("enabled"), bool):
        raise ModelInputError("dssm.time_decay must have boolean enabled")
    half_life = decay.get("half_life_seconds")
    if decay["enabled"] and (
        isinstance(half_life, bool) or not isinstance(half_life, int) or half_life < 1
    ):
        raise ModelInputError("enabled time decay requires a positive integer half_life_seconds")
    return config, sha256_bytes(canonical_json_bytes(config))
