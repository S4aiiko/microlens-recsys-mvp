from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import model_config, write_data_version
from jsonschema import Draft202012Validator

from recsys.data.artifacts import JsonLinesCodec
from recsys.data.common import canonical_json_bytes, sha256_file
from recsys.models.bundle import load_bundle
from recsys.models.data import load_model_data
from recsys.models.entrypoint import train_model
from recsys.models.errors import ModelArtifactError, ModelInputError
from recsys.models.features import FeatureIndex
from recsys.models.training import _deepfm_examples, build_dssm


class EntrypointBundleTests(unittest.TestCase):
    def test_real_two_stage_bundle_roundtrip_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_version, data_checksum = write_data_version(root / "processed")
            artifact = train_model(
                processed_root=root / "processed",
                data_version=data_version,
                data_manifest_checksum=data_checksum,
                config=model_config(),
                output_root=root / "models",
                codec=JsonLinesCodec(),
            )
            self.assertEqual(artifact.status, "READY")
            self.assertLess(artifact.bundle_path.stat().st_size, 16 * 1024 * 1024)
            bundle = load_bundle(artifact.bundle_path, artifact.manifest_checksum)
            self.assertEqual(bundle.model_version, artifact.model_version)
            self.assertEqual(bundle.manifest["negative_sampling"], "uniform")
            self.assertEqual(
                bundle.manifest["evaluation"]["candidate_policy"],
                "full_catalog_excluding_train_seen",
            )
            schema = json.loads(
                (
                    Path(__file__).parents[2] / "docs/contracts/model-manifest.schema.json"
                ).read_text()
            )
            Draft202012Validator(schema).validate(bundle.manifest)
            self.assertEqual(bundle.metrics.keys(), {"random", "popularity", "dssm", "two_stage"})
            self.assertEqual(bundle.manifest["status"], "READY")
            self.assertEqual(bundle.config["mode"], "smoke")
            checkpoint = next((root / "models" / ".checkpoints").rglob("deepfm.json"))
            checkpoint_document = json.loads(checkpoint.read_text())
            self.assertIn("best_model_state", checkpoint_document)
            self.assertIn("optimizer_state", checkpoint_document)

    def test_latest_and_wrong_checksum_fail_before_training(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_version, _checksum = write_data_version(root / "processed")
            for version, checksum in (("latest", "a" * 64), (data_version, "a" * 64)):
                with self.subTest(version=version), self.assertRaises(ModelInputError):
                    train_model(
                        processed_root=root / "processed",
                        data_version=version,
                        data_manifest_checksum=checksum,
                        config=model_config(),
                        output_root=root / "models",
                        codec=JsonLinesCodec(),
                    )

    def test_systems_only_is_evaluated_and_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed", purpose="systems_only")
            artifact = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=model_config(purpose_mode="systems"),
                output_root=root / "models",
                codec=JsonLinesCodec(),
            )
            bundle = load_bundle(artifact.bundle_path, artifact.manifest_checksum)
            self.assertEqual(bundle.manifest["status"], "EVALUATED")
            self.assertFalse(bundle.manifest["activation_eligible"])
            self.assertEqual(bundle.manifest["evaluation"]["metrics"], {})
            self.assertNotIn("two_stage", bundle.metrics)

    def test_quality_evaluation_rejects_test_rows_outside_frozen_later_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, _checksum = write_data_version(
                root / "processed", purpose="quality_evaluation"
            )
            manifest_path = root / "processed" / version / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["test_window_utc"] = {
                "from_utc": "1970-01-01T00:00:05Z",
                "to_utc": "1970-01-01T00:00:06Z",
                "interval": "[from,to)",
            }
            manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
            with self.assertRaisesRegex(ModelInputError, "outside the frozen latest window"):
                load_model_data(
                    processed_root=root / "processed",
                    data_version=version,
                    data_manifest_checksum=sha256_file(manifest_path),
                    title_config=model_config()["title"],
                    codec=JsonLinesCodec(),
                )

    def test_deepfm_training_negatives_never_label_holdout_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            config = model_config()
            data = load_model_data(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                title_config=config["title"],
                codec=JsonLinesCodec(),
            )
            features = FeatureIndex.build(data)
            examples = _deepfm_examples(build_dssm(data, config), data, features, config)
            train_items = {"i1", "i2", "i3", "i4"}
            negative_items = {
                data.item_ids[item_index]
                for _user, item_index, _source, _dense, label, _weight in examples
                if label == 0.0
            }
            self.assertTrue(negative_items)
            self.assertTrue(negative_items <= train_items)
            title_off_features = FeatureIndex.build(data, title_enabled=False)
            dense = title_off_features.dense(user_id="u1", item_id="i3", recall_score=0.5)
            self.assertEqual(dense[1], 0.0)

    def test_tampered_auxiliary_artifact_is_rejected_on_idempotent_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            arguments = {
                "processed_root": root / "processed",
                "data_version": version,
                "data_manifest_checksum": checksum,
                "config": model_config(),
                "output_root": root / "models",
                "codec": JsonLinesCodec(),
            }
            artifact = train_model(**arguments)
            (artifact.path / "metrics.json").write_text("{}\n")
            with self.assertRaisesRegex(ModelArtifactError, "(size|checksum) mismatch"):
                train_model(**arguments)

    def test_tampered_embedded_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            artifact = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=model_config(),
                output_root=root / "models",
                codec=JsonLinesCodec(),
            )
            document = json.loads(artifact.bundle_path.read_text())
            document["resolved_config"]["seed"] = 999
            artifact.bundle_path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ModelArtifactError, "config checksum mismatch"):
                load_bundle(artifact.bundle_path, artifact.manifest_checksum)

    def test_bundle_reuses_strict_config_semantics_before_state_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            artifact = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=model_config(),
                output_root=root / "models",
                codec=JsonLinesCodec(),
            )
            document = json.loads(artifact.bundle_path.read_text())
            document["resolved_config"]["dssm"]["time_decay"] = {
                "enabled": True,
                "half_life_seconds": True,
            }
            target = root / "invalid-semantic-config.json"
            target.write_text(json.dumps(document))
            with self.assertRaisesRegex(ModelArtifactError, "semantic validation failed"):
                load_bundle(target, artifact.manifest_checksum)

    def test_every_serving_payload_component_is_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            artifact = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=model_config(),
                output_root=root / "models",
                codec=JsonLinesCodec(),
            )
            original = json.loads(artifact.bundle_path.read_text())

            def change_title(document):
                document["title_encoder"]["document_frequency"][0] += 1

            def change_users(document):
                document["user_ids"].reverse()

            def change_items(document):
                document["item_ids"].reverse()

            def change_popularity(document):
                first = next(iter(document["train_popularity"]))
                document["train_popularity"][first] += 1.0

            def change_dssm(document):
                document["dssm_state"] = {}

            def change_deepfm(document):
                document["deepfm_state"] = {}

            def change_metrics(document):
                document["metrics"]["forged"] = {"ndcg@20": 1.0}

            mutations = {
                "title_encoder": change_title,
                "user_ids": change_users,
                "item_ids": change_items,
                "train_popularity": change_popularity,
                "dssm_state": change_dssm,
                "deepfm_state": change_deepfm,
                "metrics": change_metrics,
            }
            for name, mutate in mutations.items():
                document = copy.deepcopy(original)
                mutate(document)
                tampered = root / f"tampered-{name}.json"
                tampered.write_text(json.dumps(document))
                with (
                    self.subTest(name=name),
                    mock.patch("recsys.models.bundle.decode_state_dict") as decode,
                    self.assertRaisesRegex(ModelArtifactError, "payload checksum mismatch"),
                ):
                    load_bundle(tampered, artifact.manifest_checksum)
                decode.assert_not_called()

    def test_ids_popularity_and_metrics_are_strict_before_model_construction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            artifact = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=model_config(),
                output_root=root / "models",
                codec=JsonLinesCodec(),
            )
            original = json.loads(artifact.bundle_path.read_text())

            def integer_user(document):
                document["user_ids"][0] = 1

            def boolean_item(document):
                document["item_ids"][0] = True

            def empty_item(document):
                document["item_ids"][0] = ""

            def duplicate_user(document):
                document["user_ids"][1] = document["user_ids"][0]

            def oversized_item(document):
                document["item_ids"][0] = "i" * 256

            def empty_popularity(document):
                document["train_popularity"] = {}

            def unknown_popularity_key(document):
                document["train_popularity"]["not-in-catalog"] = 1.0

            def boolean_popularity(document):
                first = next(iter(document["train_popularity"]))
                document["train_popularity"][first] = True

            def string_popularity(document):
                first = next(iter(document["train_popularity"]))
                document["train_popularity"][first] = "1.0"

            def negative_popularity(document):
                first = next(iter(document["train_popularity"]))
                document["train_popularity"][first] = -1.0

            def infinite_popularity(document):
                first = next(iter(document["train_popularity"]))
                document["train_popularity"][first] = float("inf")

            def nan_metric(document):
                document["metrics"]["dssm"]["ndcg@20"] = float("nan")

            def boolean_metric(document):
                document["metrics"]["dssm"]["ndcg@20"] = True

            def string_metric(document):
                document["metrics"]["dssm"]["ndcg@20"] = "0.5"

            def string_title_bucket_count(document):
                document["title_encoder"]["bucket_count"] = "8"

            def boolean_title_frequency(document):
                document["title_encoder"]["document_frequency"][0] = True

            def integer_title_item(document):
                document["title_encoder"]["fitted_item_ids"][0] = 1

            cases = {
                "integer_user": (integer_user, "user_ids"),
                "boolean_item": (boolean_item, "item_ids"),
                "empty_item": (empty_item, "item_ids"),
                "duplicate_user": (duplicate_user, "duplicates"),
                "oversized_item": (oversized_item, "item_ids"),
                "empty_popularity": (empty_popularity, "non-empty object"),
                "unknown_popularity_key": (unknown_popularity_key, "invalid item key"),
                "boolean_popularity": (boolean_popularity, "finite non-negative"),
                "string_popularity": (string_popularity, "finite non-negative"),
                "negative_popularity": (negative_popularity, "finite non-negative"),
                "infinite_popularity": (infinite_popularity, "finite non-negative"),
                "nan_metric": (nan_metric, "finite numbers"),
                "boolean_metric": (boolean_metric, "finite numbers"),
                "string_metric": (string_metric, "finite numbers"),
                "string_title_bucket_count": (
                    string_title_bucket_count,
                    "title_encoder is invalid",
                ),
                "boolean_title_frequency": (
                    boolean_title_frequency,
                    "title_encoder is invalid",
                ),
                "integer_title_item": (integer_title_item, "title_encoder is invalid"),
            }
            for name, (mutate, message) in cases.items():
                document = copy.deepcopy(original)
                mutate(document)
                target = root / f"strict-{name}.json"
                target.write_text(json.dumps(document))
                with (
                    self.subTest(name=name),
                    mock.patch("recsys.models.bundle.decode_state_dict") as decode,
                    self.assertRaisesRegex(ModelArtifactError, message),
                ):
                    load_bundle(target, artifact.manifest_checksum)
                decode.assert_not_called()

    def test_identity_model_version_and_evidence_are_derived_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            artifact = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=model_config(),
                output_root=root / "models",
                codec=JsonLinesCodec(),
            )
            original = json.loads(artifact.bundle_path.read_text())
            cases = {
                "stage_identity": lambda row: row["manifest"]["model_identity"].update(
                    {"stage_execution_checksum": "f" * 64}
                ),
                "model_version": lambda row: row.update({"model_version": "model-forged"}),
                "evidence": lambda row: row["fixture_evidence"].update(
                    {"kind": "systems_only_two_stage"}
                ),
            }
            for name, mutate in cases.items():
                document = copy.deepcopy(original)
                mutate(document)
                if name == "stage_identity":
                    # Keep the embedded manifest checksum internally coherent to reach
                    # the independently recomputed model-version gate.
                    document["manifest_checksum"] = sha256_file(
                        self._write_manifest_fixture(root, document["manifest"])
                    )
                    expected = document["manifest_checksum"]
                else:
                    expected = artifact.manifest_checksum
                target = root / f"identity-{name}.json"
                target.write_text(json.dumps(document))
                with self.subTest(name=name), self.assertRaises(ModelArtifactError):
                    load_bundle(target, expected)

    @staticmethod
    def _write_manifest_fixture(root: Path, manifest: dict[str, object]) -> Path:
        path = root / "tampered-manifest.json"
        path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        return path

    def test_exact_checkpoint_identity_can_resume_both_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version, checksum = write_data_version(root / "processed")
            config = model_config()
            first = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=config,
                output_root=root / "models-a",
                codec=JsonLinesCodec(),
            )
            checkpoint_root = root / "models-a" / ".checkpoints"
            dssm_checkpoint = next(checkpoint_root.rglob("dssm.json"))
            deepfm_checkpoint = next(checkpoint_root.rglob("deepfm.json"))
            resumed = train_model(
                processed_root=root / "processed",
                data_version=version,
                data_manifest_checksum=checksum,
                config=config,
                output_root=root / "models-b",
                resume_dssm=dssm_checkpoint,
                resume_deepfm=deepfm_checkpoint,
                codec=JsonLinesCodec(),
            )
            first_bundle = load_bundle(first.bundle_path, first.manifest_checksum)
            resumed_bundle = load_bundle(resumed.bundle_path, resumed.manifest_checksum)
            for name, tensor in first_bundle.dssm.state_dict().items():
                self.assertTrue(tensor.equal(resumed_bundle.dssm.state_dict()[name]))
            for name, tensor in first_bundle.deepfm.state_dict().items():
                self.assertTrue(tensor.equal(resumed_bundle.deepfm.state_dict()[name]))
            resumed_training = json.loads((resumed.path / "stage_training.json").read_text())
            self.assertEqual(resumed_training["dssm"]["resumed_from_epoch"], 1)
            self.assertEqual(resumed_training["deepfm"]["resumed_from_epoch"], 1)


if __name__ == "__main__":
    unittest.main()
