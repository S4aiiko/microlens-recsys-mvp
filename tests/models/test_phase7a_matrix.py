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
from recsys.models.entrypoint import finalize_trained_model, train_model_stages
from recsys.models.errors import ModelInputError
from recsys.models.metrics import ACTIVITY_SEGMENTS

from ._support import model_config, write_data_version

REPOSITORY = Path(__file__).resolve().parents[2]


def _matrix_repo(root: Path) -> Path:
    target = root / "configs" / "models"
    target.mkdir(parents=True)
    shutil.copyfile(REPOSITORY / "configs/models/full-a.json", target / "full-a.json")
    shutil.copyfile(
        REPOSITORY / "configs/models/experiment-matrix.json",
        target / "experiment-matrix.json",
    )
    return target / "experiment-matrix.json"


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
        assert record["final_test"]["test_evaluation_count"] == 1
        assert record["final_test"]["bundle_checksum"] == sha256_file(bundle)
        by_id = {row["experiment_id"]: row for row in record["validation_runs"]}
        assert by_id["negative-uniform"]["execution_reused"] is False
        assert by_id["decay-off"]["execution_reused"] is True
        assert by_id["decay-off"]["reused_execution_from"] == "negative-uniform"
        assert by_id["decay-off"]["metrics"] == by_id["negative-uniform"]["metrics"]


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
        original = {"schema_version": "1.0", "status": "PASS", "run_id": "existing"}
        (run_path / "run.json").write_bytes(canonical_json_bytes(original) + b"\n")
        with pytest.raises(FileExistsError):
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
