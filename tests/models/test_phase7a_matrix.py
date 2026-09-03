from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from recsys.data.artifacts import JsonLinesCodec
from recsys.data.common import canonical_json_bytes, sha256_file
from recsys.experiments.phase7a import (
    evaluate_serving_ablations,
    resolve_matrix,
    run_phase7a,
)
from recsys.models.config import load_model_config
from recsys.models.data import load_model_data
from recsys.models.entrypoint import (
    TrainedModelStages,
    evaluate_validation_selection,
    finalize_trained_model,
    load_trained_model_test_split,
    train_model_stages,
)
from recsys.models.errors import ModelInputError
from recsys.models.features import FeatureIndex
from recsys.models.metrics import ACTIVITY_SEGMENTS
from recsys.models.sampling import TrainOnlyNegativeSampler

from ._support import model_config, write_data_version

REPOSITORY = Path(__file__).resolve().parents[2]
IMAGE_REFERENCE = "worker@sha256:" + "d" * 64
SOURCE_CHECKSUM = "e" * 64
RUNTIME_IDENTITY = {
    "git_revision": "c" * 40,
    "image_reference": IMAGE_REFERENCE,
    "image_digest": "sha256:" + "d" * 64,
    "source_checksum": SOURCE_CHECKSUM,
    "baked_git_revision": "c" * 40,
    "baked_source_checksum": SOURCE_CHECKSUM,
    "recomputed_source_checksum": SOURCE_CHECKSUM,
    "matrix_checksum": "f" * 64,
    "base_config_checksum": "0" * 64,
}


def _provenance_arguments(root: Path) -> dict[str, object]:
    return {
        "image_digest": IMAGE_REFERENCE,
        "requested_source_checksum": SOURCE_CHECKSUM,
        "attestation_path": root / "attestation.json",
    }


class ReadSpyCodec(JsonLinesCodec):
    def __init__(self) -> None:
        self.read_names: list[str] = []

    def read_rows(self, path: Path) -> list[dict[str, object]]:
        self.read_names.append(path.name)
        return super().read_rows(path)


def _matrix_repo(root: Path) -> Path:
    target = root / "configs" / "models"
    target.mkdir(parents=True)
    shutil.copyfile(REPOSITORY / "configs/models/full-a.json", target / "full-a.json")
    shutil.copyfile(
        REPOSITORY / "configs/models/experiment-matrix.json",
        target / "experiment-matrix.json",
    )
    return target / "experiment-matrix.json"


def _rewrite_test_artifact(
    processed_root: Path, data_version: str, payload: bytes, *, rows: int
) -> str:
    version_root = processed_root / data_version
    test_path = version_root / "test.jsonl"
    test_path.write_bytes(payload)
    manifest_path = version_root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    descriptor = next(row for row in manifest["artifacts"] if row["path"] == "test.jsonl")
    descriptor.update(
        {"size_bytes": test_path.stat().st_size, "sha256": sha256_file(test_path), "rows": rows}
    )
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    return sha256_file(manifest_path)


def test_matrix_resolution_is_deterministic_and_complete() -> None:
    first = resolve_matrix("configs/models/experiment-matrix.json", repo_root=REPOSITORY)
    second = resolve_matrix("configs/models/experiment-matrix.json", repo_root=REPOSITORY)
    assert first == second
    assert len(first.experiments) == 11
    assert {row.override_path for row in first.experiments} == {
        "dssm.negative_sampling",
        "dssm.time_decay",
        "title.enabled",
        "dssm",
        "deepfm",
    }
    assert first.selection_metric == "two_stage.ndcg@20"
    checksums = [row.config_checksum for row in first.experiments]
    assert len(set(checksums)) == 7
    controls = {
        row.experiment_id: row.config_checksum
        for row in first.experiments
        if row.experiment_id
        in {"negative-uniform", "decay-off", "title-on", "dssm-hparam-a", "deepfm-hparam-a"}
    }
    assert len(set(controls.values())) == 1


def test_matrix_unknown_override_and_unknown_fields_fail_closed() -> None:
    with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
        root = Path(temporary)
        matrix_path = _matrix_repo(root)
        document = json.loads(matrix_path.read_text())
        document["model_experiments"][0]["override_path"] = "evaluation.test_leak"
        matrix_path.write_bytes(canonical_json_bytes(document) + b"\n")
        with pytest.raises(ModelInputError, match="not allowed"):
            resolve_matrix("configs/models/experiment-matrix.json", repo_root=root)

        document["model_experiments"][0]["override_path"] = "dssm.negative_sampling"
        document["model_experiments"][0]["unexpected"] = True
        matrix_path.write_bytes(canonical_json_bytes(document) + b"\n")
        with pytest.raises(ModelInputError, match="unknown or missing"):
            resolve_matrix("configs/models/experiment-matrix.json", repo_root=root)

        del document["model_experiments"][0]["unexpected"]
        matrix_path.write_bytes(canonical_json_bytes(document) + b"\n")
        base_path = root / "configs/models/full-a.json"
        base = json.loads(base_path.read_text())
        base["unreviewed_field"] = True
        base_path.write_bytes(canonical_json_bytes(base) + b"\n")
        with pytest.raises(ModelInputError, match="model config.*unknown or missing"):
            resolve_matrix("configs/models/experiment-matrix.json", repo_root=root)


def test_matrix_rejects_serving_ablation_contract_drift() -> None:
    with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
        root = Path(temporary)
        matrix_path = _matrix_repo(root)
        document = json.loads(matrix_path.read_text())
        document["serving_ablation"]["experiments"][1]["enabled_sources"].append("dssm")
        matrix_path.write_bytes(canonical_json_bytes(document) + b"\n")
        with pytest.raises(ModelInputError, match="contract drifted"):
            resolve_matrix("configs/models/experiment-matrix.json", repo_root=root)


def test_runner_selects_on_validation_then_finalizes_test_exactly_once() -> None:
    with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
        root = Path(temporary)
        _matrix_repo(root)
        stage = SimpleNamespace(
            best_epoch=1,
            best_validation_metric=0.5,
            stop_reason="patience_exhausted",
            history=({"epoch": 0, "validation_metric": 0.5},),
            resumed_from_epoch=None,
        )

        def trained_for(**kwargs):
            return SimpleNamespace(
                config=kwargs["config"],
                config_checksum=load_model_config(kwargs["config"])[1],
                dssm_stage=stage,
                deepfm_stage=stage,
            )

        def validation(trained):
            value = 0.9 if trained.config["title"]["enabled"] is False else 0.5
            return {"dssm": {"ndcg@20": value}, "two_stage": {"ndcg@20": value}}

        artifact_root = root / "artifact"
        artifact_root.mkdir()
        bundle = artifact_root / "bundle.json"
        bundle.write_text("{}\n")
        (artifact_root / "metrics.json").write_text(
            json.dumps({"two_stage": {"ndcg@20": 0.75}}) + "\n"
        )

        def finalized(trained, *, output_root, git_revision):
            return SimpleNamespace(
                path=artifact_root,
                bundle_path=bundle,
                model_version="model-test",
                manifest_checksum="a" * 64,
                manifest={
                    "git_revision": git_revision,
                    "data_version": "data-v1",
                    "data_manifest_checksum": "b" * 64,
                    "resolved_config_checksum": trained.config_checksum,
                },
            )

        with (
            mock.patch(
                "recsys.models.entrypoint.train_model_stages", side_effect=trained_for
            ) as train,
            mock.patch(
                "recsys.models.entrypoint.evaluate_validation_selection",
                side_effect=validation,
            ) as validate,
            mock.patch(
                "recsys.models.entrypoint.finalize_trained_model", side_effect=finalized
            ) as finalize,
            mock.patch(
                "recsys.experiments.phase7a.evaluate_serving_ablations",
                return_value={"cohort": {"user_count": 1}, "experiments": {}},
            ),
            mock.patch("recsys.experiments.phase7a._environment", return_value={}),
            mock.patch(
                "recsys.experiments.phase7a._verify_runtime_identity",
                return_value=RUNTIME_IDENTITY,
            ),
            mock.patch(
                "recsys.experiments.phase7a._runtime_envelope", return_value={"verified": True}
            ),
            mock.patch("recsys.models.data.validate_data_manifest_identity", return_value={}),
            mock.patch(
                "recsys.models.entrypoint.load_trained_model_test_split",
                side_effect=lambda trained, **_kwargs: trained,
            ),
        ):
            result = run_phase7a(
                matrix_path="configs/models/experiment-matrix.json",
                repo_root=root,
                processed_root=root / "processed",
                data_version="data-v1",
                data_manifest_checksum="b" * 64,
                output_root=root / "runs",
                run_id="run-1",
                git_revision="c" * 40,
                command=["phase7a", "run"],
                **_provenance_arguments(root),
            )

        record = json.loads(result.read_text())
        unique_checksums = {
            row.config_checksum
            for row in resolve_matrix(
                "configs/models/experiment-matrix.json", repo_root=root
            ).experiments
        }
        assert train.call_count == len(unique_checksums) + 1
        assert validate.call_count == len(unique_checksums)
        finalize.assert_called_once()
        assert record["selection"]["experiment_id"] == "title-off"
        assert record["selection"]["frozen_before_test"] is True
        assert record["runtime_envelope"] == {"verified": True}
        assert record["final_test"]["test_evaluation_count"] == 1
        assert record["final_test"]["bundle_checksum"] == sha256_file(bundle)
        by_id = {row["experiment_id"]: row for row in record["validation_runs"]}
        assert by_id["negative-uniform"]["execution_reused"] is False
        assert by_id["decay-off"]["execution_reused"] is True
        assert by_id["decay-off"]["reused_execution_from"] == "negative-uniform"
        assert by_id["decay-off"]["metrics"] == by_id["negative-uniform"]["metrics"]


def test_runner_deserializes_test_once_after_selection_and_reuses_cohort() -> None:
    with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
        root = Path(temporary)
        _matrix_repo(root)
        version, checksum = write_data_version(root / "processed")
        codec = ReadSpyCodec()
        stage = SimpleNamespace(
            best_epoch=1,
            best_validation_metric=0.5,
            stop_reason="patience_exhausted",
            history=({"epoch": 0, "validation_metric": 0.5},),
            resumed_from_epoch=None,
        )

        def trained_for(**kwargs):
            config, config_checksum = load_model_config(kwargs["config"])
            data = load_model_data(
                processed_root=kwargs["processed_root"],
                data_version=kwargs["data_version"],
                data_manifest_checksum=kwargs["data_manifest_checksum"],
                title_config=config["title"],
                codec=kwargs["codec"],
                include_test=False,
            )
            assert data.test_loaded is False
            assert "test.jsonl" not in codec.read_names
            return TrainedModelStages(
                data=data,
                features=None,
                config=config,
                config_checksum=config_checksum,
                dssm=None,
                deepfm=None,
                dssm_stage=stage,
                deepfm_stage=stage,
            )

        def validation(_trained):
            assert "test.jsonl" not in codec.read_names
            return {"dssm": {"ndcg@20": 0.5}, "two_stage": {"ndcg@20": 0.5}}

        artifact_root = root / "artifact"
        artifact_root.mkdir()
        bundle = artifact_root / "bundle.json"
        bundle.write_text("{}\n")
        (artifact_root / "metrics.json").write_text(
            json.dumps({"two_stage": {"ndcg@20": 0.75}}) + "\n"
        )
        cohort_ids: list[int] = []
        real_load_test = load_trained_model_test_split

        def load_test(trained, **kwargs):
            record = json.loads((root / "runs/run-lazy/run.json").read_text())
            assert record["selection"]["frozen_before_test"] is True
            loaded = real_load_test(trained, **kwargs)
            cohort_ids.append(id(loaded.data.test))
            return loaded

        def finalized(trained, *, output_root, git_revision):
            assert trained.data.test_loaded is True
            cohort_ids.append(id(trained.data.test))
            return SimpleNamespace(
                path=artifact_root,
                bundle_path=bundle,
                model_version="model-test",
                manifest_checksum="a" * 64,
                manifest={
                    "git_revision": git_revision,
                    "data_version": version,
                    "data_manifest_checksum": checksum,
                    "resolved_config_checksum": trained.config_checksum,
                },
            )

        def ablations(trained, **_kwargs):
            cohort_ids.append(id(trained.data.test))
            return {"cohort": {"user_count": len(trained.data.test)}, "experiments": {}}

        with (
            mock.patch("recsys.models.entrypoint.train_model_stages", side_effect=trained_for),
            mock.patch(
                "recsys.models.entrypoint.evaluate_validation_selection", side_effect=validation
            ),
            mock.patch(
                "recsys.models.entrypoint.load_trained_model_test_split", side_effect=load_test
            ),
            mock.patch("recsys.models.entrypoint.finalize_trained_model", side_effect=finalized),
            mock.patch(
                "recsys.experiments.phase7a.evaluate_serving_ablations", side_effect=ablations
            ),
            mock.patch("recsys.experiments.phase7a._environment", return_value={}),
            mock.patch(
                "recsys.experiments.phase7a._verify_runtime_identity",
                return_value=RUNTIME_IDENTITY,
            ),
            mock.patch(
                "recsys.experiments.phase7a._runtime_envelope", return_value={"verified": True}
            ),
        ):
            run_phase7a(
                matrix_path="configs/models/experiment-matrix.json",
                repo_root=root,
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                output_root=root / "runs",
                run_id="run-lazy",
                git_revision="c" * 40,
                command=["phase7a", "run"],
                codec=codec,
                **_provenance_arguments(root),
            )

        assert codec.read_names.count("test.jsonl") == 1
        assert len(cohort_ids) == 3
        assert len(set(cohort_ids)) == 1


def test_changing_only_test_rows_does_not_change_training_or_selection_state() -> None:
    with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
        root = Path(temporary)
        first_version, first_checksum = write_data_version(root / "first")
        second_version, _second_checksum = write_data_version(root / "second")
        changed_rows = [
            {"user_id": "u1", "item_id": "i8", "timestamp": 4000},
            {"user_id": "u2", "item_id": "i7", "timestamp": 4000},
        ]
        payload = b"".join(canonical_json_bytes(row) + b"\n" for row in changed_rows)
        second_checksum = _rewrite_test_artifact(
            root / "second", second_version, payload, rows=len(changed_rows)
        )
        first = train_model_stages(
            processed_root=root / "first",
            data_version=first_version,
            data_manifest_checksum=first_checksum,
            config=model_config(),
            output_root=root / "models-first",
            codec=JsonLinesCodec(),
        )
        second = train_model_stages(
            processed_root=root / "second",
            data_version=second_version,
            data_manifest_checksum=second_checksum,
            config=model_config(),
            output_root=root / "models-second",
            codec=JsonLinesCodec(),
        )
        assert first.data.test_loaded is second.data.test_loaded is False
        assert first.data.train == second.data.train
        assert first.data.validation == second.data.validation
        assert first.data.train_popularity == second.data.train_popularity
        assert first.data.user_train_items == second.data.user_train_items
        assert first.data.user_history_titles == second.data.user_history_titles
        assert first.data.title_encoder.as_dict() == second.data.title_encoder.as_dict()
        assert first.data.title_encoder.checksum == second.data.title_encoder.checksum
        train_item_ids = tuple(sorted({str(row["item_id"]) for row in first.data.train}))
        assert {item_id: first.data.encoded_titles[item_id] for item_id in train_item_ids} == {
            item_id: second.data.encoded_titles[item_id] for item_id in train_item_ids
        }
        first_features = FeatureIndex.build(first.data)
        second_features = FeatureIndex.build(second.data)
        assert first_features.user_to_index == second_features.user_to_index
        assert first_features.item_to_index == second_features.item_to_index
        assert first_features.normalized_popularity == second_features.normalized_popularity
        assert first_features.normalized_activity == second_features.normalized_activity

        def sampled_negatives(
            trained: TrainedModelStages,
        ) -> dict[tuple[object, ...], tuple[str, ...]]:
            sampler = TrainOnlyNegativeSampler(
                train_item_ids=train_item_ids,
                popularity=trained.data.train_popularity,
                title_features=trained.data.encoded_titles,
                alpha=float(trained.config["dssm"]["popularity_alpha"]),
            )
            return {
                (strategy, epoch, str(row["user_id"]), str(row["item_id"])): tuple(
                    sampler.sample(
                        user_id=str(row["user_id"]),
                        positive_item_id=str(row["item_id"]),
                        seen_item_ids=set(trained.data.user_train_items[str(row["user_id"])]),
                        count=int(trained.config["dssm"]["negatives_per_positive"]),
                        seed=int(trained.config["seed"]) + epoch,
                        strategy=strategy,
                    )
                )
                for strategy in ("uniform", "popularity_aware", "train_only_hard")
                for epoch in range(2)
                for row in trained.data.train
            }

        assert sampled_negatives(first) == sampled_negatives(second)
        assert first.dssm_stage == second.dssm_stage
        assert first.deepfm_stage == second.deepfm_stage
        assert evaluate_validation_selection(first) == evaluate_validation_selection(second)

        def run_real_selection(
            processed_root: Path, checksum: str, run_id: str
        ) -> dict[str, object]:
            with (
                mock.patch(
                    "recsys.experiments.phase7a._verify_runtime_identity",
                    return_value=RUNTIME_IDENTITY,
                ),
                mock.patch(
                    "recsys.experiments.phase7a._runtime_envelope",
                    return_value={"verified": True},
                ),
            ):
                result_path = run_phase7a(
                    matrix_path="configs/models/experiment-matrix.json",
                    repo_root=REPOSITORY,
                    processed_root=processed_root,
                    data_version=first_version,
                    data_manifest_checksum=checksum,
                    output_root=root / f"runs-{run_id}",
                    run_id=run_id,
                    git_revision="c" * 40,
                    command=["phase7a", "run"],
                    codec=JsonLinesCodec(),
                    **_provenance_arguments(root),
                )
            return json.loads(result_path.read_text())

        first_run = run_real_selection(root / "first", first_checksum, "test-original")
        second_run = run_real_selection(root / "second", second_checksum, "test-mutated")
        for field in ("experiment_id", "config_checksum", "metric", "value"):
            assert first_run["selection"][field] == second_run["selection"][field]
        matrix = resolve_matrix("configs/models/experiment-matrix.json", repo_root=REPOSITORY)
        selected = next(
            row
            for row in matrix.experiments
            if row.experiment_id == first_run["selection"]["experiment_id"]
        )
        assert first_run["selection"]["config_checksum"] == selected.config_checksum
        assert first_run["selection"]["metric"] == matrix.selection_metric
        assert first_run["selection"]["frozen_before_test"] is True
        assert second_run["selection"]["frozen_before_test"] is True


@pytest.mark.parametrize("payload,rows", [(b"", 0), (b"{not-json}\n", 1)])
def test_empty_or_malformed_test_fails_only_at_final_test_load(
    tmp_path: Path, payload: bytes, rows: int
) -> None:
    version, _checksum = write_data_version(tmp_path / "processed")
    checksum = _rewrite_test_artifact(tmp_path / "processed", version, payload, rows=rows)
    trained = train_model_stages(
        processed_root=tmp_path / "processed",
        data_version=version,
        data_manifest_checksum=checksum,
        config=model_config(),
        output_root=tmp_path / "models",
        codec=JsonLinesCodec(),
    )
    evaluate_validation_selection(trained)
    with pytest.raises((ModelInputError, json.JSONDecodeError)):
        load_trained_model_test_split(
            trained, processed_root=tmp_path / "processed", codec=JsonLinesCodec()
        )


def test_runner_records_atomic_failure_with_completed_validations() -> None:
    with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
        root = Path(temporary)
        _matrix_repo(root)
        stage = SimpleNamespace(
            best_epoch=1,
            best_validation_metric=0.5,
            stop_reason="patience_exhausted",
            history=(),
            resumed_from_epoch=None,
        )

        def trained_for(**kwargs):
            return SimpleNamespace(
                config=kwargs["config"],
                config_checksum=load_model_config(kwargs["config"])[1],
                dssm_stage=stage,
                deepfm_stage=stage,
            )

        validation_calls = 0

        def validation(_trained):
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 2:
                raise RuntimeError("injected\nvalidation failure" + "x" * 600)
            return {"dssm": {"ndcg@20": 0.5}, "two_stage": {"ndcg@20": 0.5}}

        with (
            mock.patch("recsys.models.entrypoint.train_model_stages", side_effect=trained_for),
            mock.patch(
                "recsys.models.entrypoint.evaluate_validation_selection", side_effect=validation
            ),
            mock.patch("recsys.experiments.phase7a._environment", return_value={}),
            mock.patch(
                "recsys.experiments.phase7a._verify_runtime_identity",
                return_value=RUNTIME_IDENTITY,
            ),
            mock.patch(
                "recsys.experiments.phase7a._runtime_envelope", return_value={"verified": True}
            ),
            mock.patch("recsys.models.data.validate_data_manifest_identity", return_value={}),
        ):
            with pytest.raises(RuntimeError, match="injected"):
                run_phase7a(
                    matrix_path="configs/models/experiment-matrix.json",
                    repo_root=root,
                    processed_root=root / "processed",
                    data_version="data-v1",
                    data_manifest_checksum="b" * 64,
                    output_root=root / "runs",
                    run_id="failed-run",
                    git_revision="c" * 40,
                    command=["phase7a", "run"],
                    **_provenance_arguments(root),
                )

        record = json.loads((root / "runs/failed-run/run.json").read_text())
        assert record["status"] == "FAILED"
        assert record["failure_type"] == "RuntimeError"
        assert "\n" not in record["failure_message"]
        assert len(record["failure_message"]) <= 500
        assert record["completed_validation_runs"] == 1
        assert len(record["validation_runs"]) == 1
        assert record["elapsed_seconds"] >= 0


def test_runner_does_not_overwrite_an_existing_run_record() -> None:
    with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
        root = Path(temporary)
        _matrix_repo(root)
        run_path = root / "runs/existing"
        run_path.mkdir(parents=True)
        marker = {
            "schema_version": "1.0",
            "git_revision": "c" * 40,
            "image_reference": IMAGE_REFERENCE,
            "image_digest": "sha256:" + "d" * 64,
            "source_checksum": SOURCE_CHECKSUM,
            "matrix_checksum": "f" * 64,
            "base_config_checksum": "0" * 64,
            "data_version": "data-v1",
            "data_manifest_checksum": "b" * 64,
        }
        (root / "runs/.phase7a-namespace.json").write_bytes(canonical_json_bytes(marker) + b"\n")
        original = {"schema_version": "1.0", "status": "PASS", "run_id": "existing"}
        (run_path / "run.json").write_bytes(canonical_json_bytes(original) + b"\n")
        with (
            mock.patch(
                "recsys.experiments.phase7a._verify_runtime_identity",
                return_value=RUNTIME_IDENTITY,
            ),
            mock.patch(
                "recsys.experiments.phase7a._runtime_envelope", return_value={"verified": True}
            ),
            mock.patch("recsys.models.data.validate_data_manifest_identity", return_value={}),
            pytest.raises(ModelInputError, match="already claimed"),
        ):
            run_phase7a(
                matrix_path="configs/models/experiment-matrix.json",
                repo_root=root,
                processed_root=root / "processed",
                data_version="data-v1",
                data_manifest_checksum="b" * 64,
                output_root=root / "runs",
                run_id="existing",
                git_revision="c" * 40,
                command=["phase7a", "run"],
                **_provenance_arguments(root),
            )
        assert json.loads((run_path / "run.json").read_text()) == original


def test_serving_ablation_entrypoint_runs_without_database_or_redis() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        version, checksum = write_data_version(root / "processed")
        trained = train_model_stages(
            processed_root=root / "processed",
            data_version=version,
            data_manifest_checksum=checksum,
            config=model_config(),
            output_root=root / "models",
            codec=JsonLinesCodec(),
        )
        trained = load_trained_model_test_split(
            trained,
            processed_root=root / "processed",
            codec=JsonLinesCodec(),
        )
        artifact = finalize_trained_model(trained, output_root=root / "models")
        result = evaluate_serving_ablations(
            trained,
            bundle_path=artifact.bundle_path,
            manifest_checksum=artifact.manifest_checksum,
            matrix=resolve_matrix("configs/models/experiment-matrix.json", repo_root=REPOSITORY),
        )
        assert result["cohort"]["user_count"] == 2
        assert set(result["experiments"]) == {
            "recall-all",
            "recall-without-dssm",
            "recall-without-cf",
            "recall-without-profile-title",
            "topic-dedup-on",
            "mmr-on",
        }
        for experiment in result["experiments"].values():
            assert experiment["overall"].keys() >= {
                "recall@20",
                "ndcg@20",
                "hit_rate@20",
            }
            assert set(experiment["segments"]) == set(ACTIVITY_SEGMENTS)
