from __future__ import annotations

from pathlib import Path

from recsys.data.artifacts import JsonLinesCodec
from recsys.data.common import artifact_descriptor, canonical_json_bytes, sha256_file

DENSE_FEATURES = [
    "dssm_recall_score",
    "title_history_similarity",
    "train_popularity_log_normalized",
    "train_novelty",
    "train_user_activity_log_normalized",
    "train_time_decay_weight",
]


def model_config(*, seed: int = 7, purpose_mode: str = "smoke") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "experiment_name": f"fixture-{seed}",
        "mode": purpose_mode,
        "seed": seed,
        "title": {
            "enabled": True,
            "bucket_count": 32,
            "ngram_min": 1,
            "ngram_max": 2,
            "embedding_dim": 4,
            "maximum_tokens": 6,
        },
        "dssm": {
            "embedding_dim": 4,
            "hidden_dims": [8],
            "output_dim": 4,
            "dropout": 0.0,
            "temperature": 0.2,
            "learning_rate": 0.01,
            "batch_size": 4,
            "epochs": 2,
            "patience": 1,
            "min_delta": 0.0,
            "negative_sampling": "uniform",
            "negatives_per_positive": 1,
            "popularity_alpha": 0.75,
            "candidate_top_n": 6,
            "time_decay": {"enabled": False, "half_life_seconds": 604800},
        },
        "deepfm": {
            "embedding_dim": 4,
            "hidden_dims": [8],
            "dropout": 0.0,
            "learning_rate": 0.01,
            "batch_size": 8,
            "epochs": 2,
            "patience": 1,
            "min_delta": 0.0,
            "negatives_per_positive": 2,
            "dense_features": DENSE_FEATURES,
        },
        "evaluation": {
            "candidate_policy": "full_catalog_excluding_train_seen",
            "k": [1, 3],
            "maximum_badcases": 20,
        },
    }


def write_data_version(root: Path, *, purpose: str = "base_official") -> tuple[str, str]:
    codec = JsonLinesCodec()
    data_version = f"fixture-{purpose}"
    path = root / data_version
    path.mkdir(parents=True)
    item_ids = [f"i{index}" for index in range(1, 9)]
    tables = {
        "items": [
            {
                "item_id": item_id,
                "title": f"topic {index}",
                "likes_snapshot": index,
                "views_snapshot": index * 10,
                "cover_ref": None,
                "metadata_status": "complete",
            }
            for index, item_id in enumerate(item_ids, start=1)
        ],
        "train": [
            {"user_id": "u1", "item_id": "i1", "timestamp": 1000},
            {"user_id": "u1", "item_id": "i2", "timestamp": 2000},
            {"user_id": "u2", "item_id": "i3", "timestamp": 1000},
            {"user_id": "u2", "item_id": "i4", "timestamp": 2000},
        ],
        "validation": [
            {"user_id": "u1", "item_id": "i5", "timestamp": 3000},
            {"user_id": "u2", "item_id": "i6", "timestamp": 3000},
        ],
        "test": [
            {"user_id": "u1", "item_id": "i7", "timestamp": 4000},
            {"user_id": "u2", "item_id": "i8", "timestamp": 4000},
        ],
        "train_popularity": [
            {
                "item_id": item_id,
                "count": 5 if item_id in {"i1", "i3"} else 1,
                "probability": 0.125,
                "time_decayed_count": 5.0 if item_id in {"i1", "i3"} else 1.0,
            }
            for item_id in item_ids
        ],
        "title_corpus": [
            {
                "item_id": item_id,
                "normalized_title": f"topic {index}",
                "item_split_membership": [
                    "train" if index <= 4 else "validation" if index <= 6 else "test"
                ],
                "is_train_item": index <= 4,
            }
            for index, item_id in enumerate(item_ids, start=1)
        ],
    }
    if purpose == "systems_only":
        tables["validation"] = []
        tables["test"] = []
    artifacts = []
    for name, rows in tables.items():
        target = path / f"{name}{codec.suffix}"
        codec.write_rows(target, rows)
        artifacts.append(artifact_descriptor(target, rows=len(rows)))
    manifest = {
        "schema_version": "1.0",
        "data_version": data_version,
        "purpose": purpose,
        "evaluation_comparability": (
            "non_comparable" if purpose == "systems_only" else "comparable"
        ),
        "activation_eligible": purpose != "systems_only",
        "output_schema": {"storage_format": codec.format_name},
        "artifacts": artifacts,
    }
    if purpose == "quality_evaluation":
        manifest.update(
            {
                "base_data_version": "fixture-base-official",
                "parent_manifest_checksum": "a" * 64,
                "event_export_checksum": "b" * 64,
                "event_id_watermark_range": {"start_exclusive": 0, "end_inclusive": 4},
                "event_mapping_config_checksum": "c" * 64,
                "train_cutoff_utc": "1970-01-01T00:00:02.500Z",
                "validation_window_utc": {
                    "from_utc": "1970-01-01T00:00:02.500Z",
                    "to_utc": "1970-01-01T00:00:03.500Z",
                    "interval": "[from,to)",
                },
                "test_window_utc": {
                    "from_utc": "1970-01-01T00:00:03.500Z",
                    "to_utc": "1970-01-01T00:00:04.500Z",
                    "interval": "[from,to)",
                },
                "holdout_counts": {"validation": 2, "test": 2},
            }
        )
    manifest_path = path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    return data_version, sha256_file(manifest_path)
