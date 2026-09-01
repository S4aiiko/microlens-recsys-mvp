from __future__ import annotations

import hmac
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch

from recsys.data.common import SHA256_PATTERN, canonical_json_bytes, sha256_bytes

from .config import load_model_config
from .deepfm import DeepFMRanker
from .errors import ModelArtifactError, ModelInputError
from .state import decode_state_dict
from .text import TitleHashEncoder
from .two_tower import TwoTowerModel

MAX_BUNDLE_BYTES = 16 * 1024 * 1024

IDENTITY_FIELDS = {
    "schema_version",
    "data_version",
    "data_manifest_checksum",
    "resolved_config_checksum",
    "seed",
    "title_encoder_checksum",
    "user_ids_checksum",
    "item_ids_checksum",
    "train_popularity_checksum",
    "dssm_state_checksum",
    "deepfm_state_checksum",
    "metrics_checksum",
    "stage_execution_checksum",
}


def _strict_ids(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ModelArtifactError(f"ModelBundle {label} must be a non-empty array")
    if any(not isinstance(row, str) or not 1 <= len(row) <= 255 for row in value):
        raise ModelArtifactError(f"ModelBundle {label} must contain bounded non-empty strings")
    if len(value) != len(set(value)):
        raise ModelArtifactError(f"ModelBundle {label} contains duplicates")
    return tuple(value)


def _strict_popularity(value: Any, *, item_ids: set[str]) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ModelArtifactError("ModelBundle train_popularity must be a non-empty object")
    output: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or key not in item_ids:
            raise ModelArtifactError("ModelBundle train_popularity has an invalid item key")
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int | float)
            or not math.isfinite(float(raw))
            or raw < 0
        ):
            raise ModelArtifactError(
                "ModelBundle train_popularity values must be finite non-negative numbers"
            )
        output[key] = float(raw)
    return output


def _validate_metric_tree(value: Any, *, path: str = "metrics") -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) or not key for key in value):
            raise ModelArtifactError(f"ModelBundle {path} has an invalid key")
        for key, child in value.items():
            _validate_metric_tree(child, path=f"{path}.{key}")
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
    ):
        raise ModelArtifactError(f"ModelBundle {path} must contain only finite numbers")


@dataclass(frozen=True, slots=True)
class ModelBundle:
    model_version: str
    data_version: str
    manifest_checksum: str
    config_checksum: str
    manifest: dict[str, Any]
    config: dict[str, Any]
    user_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    title_encoder: TitleHashEncoder
    popularity: dict[str, float]
    dssm: TwoTowerModel
    deepfm: DeepFMRanker
    metrics: dict[str, Any]

    @property
    def user_to_index(self) -> dict[str, int]:
        return {user_id: index for index, user_id in enumerate(self.user_ids)}

    @property
    def item_to_index(self) -> dict[str, int]:
        return {item_id: index for index, item_id in enumerate(self.item_ids)}

    @torch.no_grad()
    def smoke(self) -> None:
        if not self.user_ids or not self.item_ids:
            raise ModelArtifactError("bundle has an empty user/item map")
        self.dssm.eval()
        self.deepfm.eval()
        user = torch.tensor([0], dtype=torch.long)
        item = torch.tensor([0], dtype=torch.long)
        recall = self.dssm.pair_scores(user, item)
        dense = torch.zeros((1, 6), dtype=torch.float32)
        dense[:, 0] = recall
        rank = self.deepfm(user, item, torch.zeros(1, dtype=torch.long), dense)
        if recall.shape != (1,) or rank.shape != (1,):
            raise ModelArtifactError("bundle smoke produced invalid tensor shapes")
        if not math.isfinite(float(recall.item())) or not math.isfinite(float(rank.item())):
            raise ModelArtifactError("bundle smoke produced a non-finite score")


def _read_bundle_document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ModelArtifactError("ModelBundle must be a real regular file")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ModelArtifactError("ModelBundle size is outside the safe staging limit")
    payload = path.read_bytes()
    if path.is_symlink() or not path.is_file() or path.stat().st_size != len(payload):
        raise ModelArtifactError("ModelBundle changed while being read")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelArtifactError("ModelBundle is invalid JSON") from exc
    if not isinstance(document, dict):
        raise ModelArtifactError("ModelBundle must contain a JSON object")
    return document


def load_bundle(path: str | Path, expected_manifest_checksum: str) -> ModelBundle:
    document = _read_bundle_document(Path(path))
    required = {
        "schema_version",
        "model_version",
        "data_version",
        "manifest_checksum",
        "config_checksum",
        "fixture_evidence",
        "manifest",
        "resolved_config",
        "user_ids",
        "item_ids",
        "title_encoder",
        "train_popularity",
        "dssm_state",
        "deepfm_state",
        "metrics",
    }
    if set(document) != required:
        raise ModelArtifactError("ModelBundle fields do not match schema 1.0")
    if document["schema_version"] != "1.0":
        raise ModelArtifactError("unsupported ModelBundle schema")
    manifest = document["manifest"]
    if not isinstance(manifest, dict):
        raise ModelArtifactError("ModelBundle manifest must be an object")
    calculated_manifest_checksum = sha256_bytes(canonical_json_bytes(manifest) + b"\n")
    if not hmac.compare_digest(calculated_manifest_checksum, str(document["manifest_checksum"])):
        raise ModelArtifactError("embedded model manifest checksum mismatch")
    if not hmac.compare_digest(calculated_manifest_checksum, expected_manifest_checksum):
        raise ModelArtifactError("expected model manifest checksum mismatch")
    if document["model_version"] != manifest.get("model_version"):
        raise ModelArtifactError("ModelBundle/model manifest version mismatch")
    if document["data_version"] != manifest.get("data_version"):
        raise ModelArtifactError("ModelBundle/model manifest data version mismatch")
    if document["config_checksum"] != manifest.get("resolved_config_checksum"):
        raise ModelArtifactError("ModelBundle/model manifest config checksum mismatch")
    config = document["resolved_config"]
    if not isinstance(config, dict):
        raise ModelArtifactError("resolved config must be an object")
    try:
        config, actual_config_checksum = load_model_config(config)
    except (ModelInputError, ValueError) as exc:
        raise ModelArtifactError("resolved config semantic validation failed") from exc
    if not hmac.compare_digest(actual_config_checksum, str(document["config_checksum"])):
        raise ModelArtifactError("resolved config checksum mismatch")
    purpose = manifest.get("purpose")
    comparability = manifest.get("evaluation_comparability")
    eligible = manifest.get("activation_eligible")
    status = manifest.get("status")
    if (purpose == "systems_only" or comparability == "non_comparable") and (
        eligible is not False or status not in {"EVALUATED", "FAILED"}
    ):
        raise ModelArtifactError("non-comparable ModelBundle violates activation policy")
    user_ids = _strict_ids(document["user_ids"], label="user_ids")
    item_ids = _strict_ids(document["item_ids"], label="item_ids")
    popularity = _strict_popularity(document["train_popularity"], item_ids=set(item_ids))
    if not isinstance(document["metrics"], dict):
        raise ModelArtifactError("ModelBundle metrics must be an object")
    _validate_metric_tree(document["metrics"])
    if not isinstance(document["title_encoder"], dict):
        raise ModelArtifactError("ModelBundle title_encoder must be an object")
    try:
        title_encoder = TitleHashEncoder.from_dict(document["title_encoder"])
    except (TypeError, ValueError) as exc:
        raise ModelArtifactError("ModelBundle title_encoder is invalid") from exc
    identity = manifest.get("model_identity")
    if not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS:
        raise ModelArtifactError("model identity has unknown or missing fields")
    if identity.get("schema_version") != "1.0" or any(
        not isinstance(identity.get(field), str) or not SHA256_PATTERN.fullmatch(identity[field])
        for field in IDENTITY_FIELDS - {"schema_version", "data_version", "seed"}
    ):
        raise ModelArtifactError("model identity checksum fields are invalid")
    if (
        identity.get("data_version") != document["data_version"]
        or identity.get("data_manifest_checksum") != manifest.get("data_manifest_checksum")
        or identity.get("resolved_config_checksum") != document["config_checksum"]
        or identity.get("seed") != config.get("seed")
    ):
        raise ModelArtifactError("model identity lineage does not match bundle payload")
    recalculated_payload = {
        "title_encoder_checksum": title_encoder.checksum,
        "user_ids_checksum": sha256_bytes(canonical_json_bytes(document["user_ids"])),
        "item_ids_checksum": sha256_bytes(canonical_json_bytes(document["item_ids"])),
        "train_popularity_checksum": sha256_bytes(
            canonical_json_bytes(document["train_popularity"])
        ),
        "dssm_state_checksum": sha256_bytes(canonical_json_bytes(document["dssm_state"])),
        "deepfm_state_checksum": sha256_bytes(canonical_json_bytes(document["deepfm_state"])),
        "metrics_checksum": sha256_bytes(canonical_json_bytes(document["metrics"])),
    }
    if any(
        not hmac.compare_digest(str(identity[field]), checksum)
        for field, checksum in recalculated_payload.items()
    ):
        raise ModelArtifactError("ModelBundle serving payload checksum mismatch")
    recalculated_version = f"model-{sha256_bytes(canonical_json_bytes(identity))[:20]}"
    if not hmac.compare_digest(recalculated_version, str(document["model_version"])):
        raise ModelArtifactError("ModelBundle model_version does not match model identity")
    evidence = document["fixture_evidence"]
    expected_evidence_kind = (
        "official_smoke_two_stage"
        if purpose == "base_official" and config.get("mode") == "smoke"
        else "base_official_two_stage"
        if purpose == "base_official"
        else "quality_evaluation_two_stage"
        if purpose == "quality_evaluation"
        else "systems_only_two_stage"
    )
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"kind", "dssm", "deepfm"}
        or evidence.get("dssm") is not True
        or evidence.get("deepfm") is not True
        or evidence.get("kind") != expected_evidence_kind
    ):
        raise ModelArtifactError("ModelBundle fixture evidence is invalid")
    dssm_state = decode_state_dict(document["dssm_state"])
    deepfm_state = decode_state_dict(document["deepfm_state"])
    item_tokens = dssm_state["item_title_tokens"]
    item_weights = dssm_state["item_title_weights"]
    user_tokens = dssm_state["user_title_tokens"]
    user_weights = dssm_state["user_title_weights"]
    dssm_config = config["dssm"]
    title_config = config["title"]
    dssm = TwoTowerModel(
        user_count=len(user_ids),
        item_count=len(item_ids),
        title_bucket_count=int(title_config["bucket_count"]),
        item_title_tokens=torch.zeros_like(item_tokens),
        item_title_weights=torch.zeros_like(item_weights),
        user_title_tokens=torch.zeros_like(user_tokens),
        user_title_weights=torch.zeros_like(user_weights),
        embedding_dim=int(dssm_config["embedding_dim"]),
        title_dim=int(title_config["embedding_dim"]),
        hidden_dims=[int(value) for value in dssm_config.get("hidden_dims", [])],
        output_dim=int(dssm_config["output_dim"]),
        dropout=float(dssm_config.get("dropout", 0.0)),
        temperature=float(dssm_config.get("temperature", 0.1)),
        title_enabled=bool(title_config["enabled"]),
    )
    dssm.load_state_dict(dssm_state, strict=True)
    deepfm_config = config["deepfm"]
    deepfm = DeepFMRanker(
        user_count=len(user_ids),
        item_count=len(item_ids),
        source_count=1,
        dense_feature_count=6,
        embedding_dim=int(deepfm_config["embedding_dim"]),
        hidden_dims=[int(value) for value in deepfm_config.get("hidden_dims", [])],
        dropout=float(deepfm_config.get("dropout", 0.0)),
    )
    deepfm.load_state_dict(deepfm_state, strict=True)
    if title_encoder.checksum != manifest.get("runtime_environment", {}).get(
        "title_encoder_checksum"
    ):
        raise ModelArtifactError("title encoder checksum mismatch")
    if item_tokens.shape[0] != len(item_ids) or user_tokens.shape[0] != len(user_ids):
        raise ModelArtifactError("ModelBundle title tables do not match ID maps")
    bundle = ModelBundle(
        model_version=str(document["model_version"]),
        data_version=str(document["data_version"]),
        manifest_checksum=calculated_manifest_checksum,
        config_checksum=str(document["config_checksum"]),
        manifest=manifest,
        config=config,
        user_ids=user_ids,
        item_ids=item_ids,
        title_encoder=title_encoder,
        popularity=popularity,
        dssm=dssm,
        deepfm=deepfm,
        metrics=document["metrics"],
    )
    bundle.smoke()
    return bundle
