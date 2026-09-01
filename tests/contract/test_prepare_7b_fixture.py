from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "microlens_prepare_7b_fixture", ROOT / "scripts" / "prepare_7b_fixture.py"
)
assert SPEC is not None and SPEC.loader is not None
fixture_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fixture_module
SPEC.loader.exec_module(fixture_module)

BLOCKED_INPUT = fixture_module.BLOCKED_INPUT
INVALID_INPUT = fixture_module.INVALID_INPUT
BundleEvidence = fixture_module.BundleEvidence
FixtureError = fixture_module.FixtureError
FixtureSpec = fixture_module.FixtureSpec
prepare = fixture_module.prepare
validate_bundle = fixture_module.validate_bundle
load_or_create_credentials = fixture_module._load_or_create_credentials
fixed_event_graph = fixture_module._fixed_event_graph
registration_policy = fixture_module._registration_policy
stage_and_register = fixture_module._stage_and_register
verify_container_credentials = fixture_module._verify_container_credentials


def bundle_document(version: str, *, evidence_kind: str) -> dict[str, object]:
    return {
        "model_version": version,
        "data_version": "official-smoke-data-v1",
        "manifest_checksum": "a" * 64,
        "config_checksum": "b" * 64,
        "fixture_evidence": {
            "kind": evidence_kind,
            "dssm": True,
            "deepfm": True,
        },
    }


class NeverRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments, *, environment=None, check=True):
        self.calls.append(list(arguments))
        raise AssertionError("Docker must not be called before bundle preflight passes")


class StagingRunner:
    def __init__(self, bundles: tuple[BundleEvidence, BundleEvidence]) -> None:
        self.bundles = {bundle.model_version: bundle for bundle in bundles}
        self.streamed: list[bytes] = []

    def run(self, arguments, *, environment=None, input_payload=None, check=True):
        del environment, check
        joined = " ".join(arguments)
        if input_payload is not None:
            self.streamed.append(input_payload)
            return b""
        if "hashlib.sha256" in joined:
            return (hashlib.sha256(self.streamed[-1]).hexdigest() + "\n").encode()
        if "SELECT model_version ||" in joined:
            for version, bundle in self.bundles.items():
                if f"model_version='{version}'" in joined:
                    return (
                        f"{version}|{bundle.artifact_checksum}|{bundle.manifest_checksum}|"
                        "EVALUATED\n"
                    ).encode()
        return b""


class FixtureInputTests(unittest.TestCase):
    def test_fixture_id_is_strict_reserved_and_deterministic(self) -> None:
        first = FixtureSpec.create("abc-123")
        second = FixtureSpec.create("abc-123")
        self.assertEqual(first, second)
        self.assertEqual(first.project, "microlens-7b-abc-123")
        self.assertEqual(first.artifact_namespace, "/artifacts/experiments/7b/abc-123")
        self.assertNotEqual(first.db_port, first.redis_port)
        for invalid in ("ab", "UPPER", "../escape", "default", "microlens-mvp"):
            with self.subTest(invalid=invalid), self.assertRaises(FixtureError) as raised:
                FixtureSpec.create(invalid)
            self.assertEqual(raised.exception.exit_code, INVALID_INPUT)

    def test_missing_phase3_bundles_fail_before_any_docker_command(self) -> None:
        runner = NeverRunner()
        arguments = argparse.Namespace(
            fixture_id="safe-fixture",
            model_bundle_a="",
            model_bundle_a_sha256="",
            model_bundle_b="",
            model_bundle_b_sha256="",
            docker=None,
            protocol_test_only=False,
        )
        with self.assertRaises(FixtureError) as raised:
            prepare(arguments, runner=runner)
        self.assertEqual(raised.exception.exit_code, BLOCKED_INPUT)
        self.assertEqual(raised.exception.code, "bundle_checksum_missing")
        self.assertEqual(runner.calls, [])

    def test_checksum_valid_synthetic_bundle_is_only_protocol_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_root = root / "artifacts" / "models"
            model_root.mkdir(parents=True)
            path = model_root / "synthetic-a.json"
            path.write_text(
                json.dumps(
                    bundle_document("synthetic-model-a", evidence_kind="synthetic_protocol_test"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            evidence = validate_bundle(str(path), checksum, repo_root=root, protocol_test_only=True)
            self.assertIsInstance(evidence, BundleEvidence)
            self.assertEqual(evidence.evidence_kind, "synthetic_protocol_test")
            with self.assertRaises(FixtureError) as raised:
                validate_bundle(str(path), checksum, repo_root=root, protocol_test_only=False)
            self.assertEqual(raised.exception.code, "official_smoke_evidence_missing")

    def test_symlink_traversal_and_checksum_mismatch_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_root = root / "artifacts" / "models"
            model_root.mkdir(parents=True)
            real = model_root / "real.json"
            real.write_text(
                json.dumps(
                    bundle_document("official-model-a", evidence_kind="official_smoke_two_stage")
                ),
                encoding="utf-8",
            )
            checksum = hashlib.sha256(real.read_bytes()).hexdigest()
            link = model_root / "link.json"
            link.symlink_to(real)
            with self.assertRaises(FixtureError) as symlink_error:
                validate_bundle(str(link), checksum, repo_root=root, protocol_test_only=False)
            self.assertEqual(symlink_error.exception.code, "symlink_bundle_forbidden")
            with self.assertRaises(FixtureError) as traversal_error:
                validate_bundle(
                    "artifacts/models/../escape.json",
                    checksum,
                    repo_root=root,
                    protocol_test_only=False,
                )
            self.assertEqual(traversal_error.exception.code, "unsafe_bundle_path")
            with self.assertRaises(FixtureError) as checksum_error:
                validate_bundle(str(real), "f" * 64, repo_root=root, protocol_test_only=False)
            self.assertEqual(checksum_error.exception.code, "bundle_checksum_mismatch")

    def test_artifacts_and_models_roots_cannot_be_symlinks(self) -> None:
        for symlink_component in ("artifacts", "models"):
            with (
                self.subTest(component=symlink_component),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                outside = root / "outside"
                outside.mkdir()
                if symlink_component == "artifacts":
                    (root / "artifacts").symlink_to(outside, target_is_directory=True)
                    model_root = outside / "models"
                else:
                    artifacts = root / "artifacts"
                    artifacts.mkdir()
                    (artifacts / "models").symlink_to(outside, target_is_directory=True)
                    model_root = outside
                model_root.mkdir(exist_ok=True)
                bundle = model_root / "bundle.json"
                bundle.write_text(
                    json.dumps(
                        bundle_document(
                            "official-model-root", evidence_kind="official_smoke_two_stage"
                        )
                    ),
                    encoding="utf-8",
                )
                checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
                lexical = root / "artifacts" / "models" / "bundle.json"
                with self.assertRaises(FixtureError) as raised:
                    validate_bundle(
                        str(lexical), checksum, repo_root=root, protocol_test_only=False
                    )
                self.assertEqual(raised.exception.code, "symlink_bundle_forbidden")

    def test_models_directory_itself_is_rejected_as_a_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_root = root / "artifacts" / "models"
            model_root.mkdir(parents=True)
            with self.assertRaises(FixtureError) as raised:
                validate_bundle(
                    str(model_root),
                    hashlib.sha256(b"not-used").hexdigest(),
                    repo_root=root,
                    protocol_test_only=False,
                )
            self.assertEqual(raised.exception.code, "bundle_not_regular")

    def test_credentials_are_random_persisted_and_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = FixtureSpec.create("secret-fixture")
            first = load_or_create_credentials(root, spec, existing_docker_resources=False)
            second = load_or_create_credentials(root, spec, existing_docker_resources=False)
            self.assertEqual(first, second)
            self.assertEqual(len(set(first.values())), 4)
            path = root / "artifacts" / "experiments" / "7b" / spec.fixture_id / ".fixture-env"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(spec.fixture_id, "".join(first.values()))

    def test_every_credential_namespace_ancestor_rejects_symlinks(self) -> None:
        for symlink_component in ("artifacts", "experiments", "7b", "fixture"):
            with (
                self.subTest(component=symlink_component),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                spec = FixtureSpec.create("ancestor-fixture")
                outside = root / "outside"
                outside.mkdir()
                current = root
                components = ("artifacts", "experiments", "7b", spec.fixture_id)
                target_index = 3 if symlink_component == "fixture" else components.index(
                    symlink_component
                )
                for index, component in enumerate(components):
                    next_path = current / component
                    if index == target_index:
                        next_path.symlink_to(outside, target_is_directory=True)
                        break
                    next_path.mkdir()
                    current = next_path
                with self.assertRaises(FixtureError) as raised:
                    load_or_create_credentials(root, spec, existing_docker_resources=False)
                self.assertEqual(
                    raised.exception.code, "symlink_artifact_namespace_forbidden"
                )

    def test_validated_bytes_are_staged_even_if_original_bundle_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_root = root / "artifacts" / "models"
            model_root.mkdir(parents=True)
            evidence: list[BundleEvidence] = []
            originals: list[bytes] = []
            for suffix in ("a", "b"):
                path = model_root / f"synthetic-{suffix}.json"
                payload = json.dumps(
                    bundle_document(
                        f"synthetic-stage-{suffix}", evidence_kind="synthetic_protocol_test"
                    ),
                    sort_keys=True,
                ).encode()
                path.write_bytes(payload)
                originals.append(payload)
                evidence.append(
                    validate_bundle(
                        str(path),
                        hashlib.sha256(payload).hexdigest(),
                        repo_root=root,
                        protocol_test_only=True,
                    )
                )
                path.write_bytes(b"replaced after validation")
            bundles = (evidence[0], evidence[1])
            runner = StagingRunner(bundles)
            stage_and_register(
                runner,
                "docker",
                ["docker", "compose"],
                {},
                FixtureSpec.create("stage-fixture"),
                bundles,
                "synthetic_protocol_test",
            )
            self.assertEqual(runner.streamed, originals)

    def test_fixed_graph_is_deterministic_complete_and_model_bound(self) -> None:
        spec = FixtureSpec.create("graph-fixture")
        graph = fixed_event_graph(spec, "official-model-a")
        self.assertEqual(graph, fixed_event_graph(spec, "official-model-a"))
        self.assertEqual(graph["model_version"], "official-model-a")
        self.assertEqual(
            [event["event_type"] for event in graph["events"]],
            ["impression", "click", "like", "not_interested", "dwell", "revisit", "share"],
        )
        self.assertEqual(len({event["event_id"] for event in graph["events"]}), 7)

    def test_synthetic_registry_policy_is_ineligible_and_non_comparable(self) -> None:
        self.assertEqual(
            registration_policy("synthetic_protocol_test"),
            ("systems_only", "non_comparable", "false", "EVALUATED"),
        )
        self.assertEqual(
            registration_policy("official_smoke_two_stage"),
            ("base_official", "comparable", "true", "READY"),
        )

    def test_replaced_credential_file_cannot_match_running_containers(self) -> None:
        credentials = {
            "FIXTURE_POSTGRES_PASSWORD": "p" * 40,
            "FIXTURE_JWT_SECRET": "j" * 40,
            "FIXTURE_PUBLISH_TOKEN": "t" * 40,
            "FIXTURE_SEED_PASSWORD": "s" * 40,
        }
        environment = FixtureSpec.create("credential-check").environment(credentials)
        services = {
            "db": {"Config": {"Env": ["POSTGRES_PASSWORD=replaced"]}},
            "api": {
                "Config": {
                    "Env": [
                        f"JWT_SECRET={credentials['FIXTURE_JWT_SECRET']}",
                        f"PUBLISH_TOKEN={credentials['FIXTURE_PUBLISH_TOKEN']}",
                        f"MICROLENS_SEED_PASSWORD={credentials['FIXTURE_SEED_PASSWORD']}",
                    ]
                }
            },
        }
        with self.assertRaises(FixtureError) as raised:
            verify_container_credentials(services, environment)
        self.assertEqual(raised.exception.code, "fixture_credentials_do_not_match_containers")


class FixtureStaticSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]

    def test_compose_is_standalone_labeled_and_has_no_default_or_external_state(self) -> None:
        compose = (self.root / "scripts" / "compose.7b.yaml").read_text(encoding="utf-8")
        self.assertIn("io.microlens.environment_kind: 7b_fixture", compose)
        self.assertIn("127.0.0.1:${FIXTURE_DB_PORT", compose)
        self.assertIn("127.0.0.1:${FIXTURE_REDIS_PORT", compose)
        self.assertIn("/artifacts/experiments/7b/${FIXTURE_ID}", compose)
        self.assertNotIn("external: true", compose)
        self.assertNotIn("container_name:", compose)
        self.assertNotIn("microlens-mvp", compose)

    def test_prepare_target_requires_explicit_model_paths_and_checksums(self) -> None:
        makefile = (self.root / "Makefile").read_text(encoding="utf-8")
        target = makefile.split("prepare-7b-fixture:", 1)[1].split("\n\n", 1)[0]
        for variable in (
            "FIXTURE_ID",
            "MODEL_BUNDLE_A",
            "MODEL_BUNDLE_A_SHA256",
            "MODEL_BUNDLE_B",
            "MODEL_BUNDLE_B_SHA256",
        ):
            self.assertIn(variable, target)
        self.assertNotIn("placeholder", target)

    def test_script_has_no_destructive_docker_or_datastore_command(self) -> None:
        source = (
            (self.root / "scripts" / "prepare_7b_fixture.py").read_text(encoding="utf-8").lower()
        )
        for forbidden_call in (
            '"down"',
            '"rm"',
            '"volume", "rm"',
            '"network", "rm"',
            '"redis-cli", "flushall"',
            '"truncate table"',
        ):
            self.assertNotIn(forbidden_call, source)


if __name__ == "__main__":
    unittest.main()
