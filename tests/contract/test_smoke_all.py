from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.smoke_all import (
    BUILDX_SESSION_ERROR,
    COMPOSE_FILES,
    COMPOSE_RECONCILIATION_ERRORS,
    COMPOSE_RECONCILIATION_MAX_ATTEMPTS,
    EXPECTED_SERVICES,
    CommandResult,
    SmokeError,
    SmokeOrchestrator,
    SmokeSpec,
)

CHECKSUM = "a" * 64
MODEL_CHECKSUM = "b" * 64
DATA_VERSION = "microlens50k-smoke-test"
MODEL_VERSION = "model-smoke-test"


class FakeRunner:
    def __init__(self, *, fail_phase: str | None = None) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str], int]] = []
        self.fail_phase = fail_phase
        self.overrides: dict[str, bytes] = {}
        self.results: dict[str, list[CommandResult]] = {}

    def run(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str],
        timeout: int,
        phase: str,
        check: bool = True,
    ) -> CommandResult:
        self.calls.append((phase, arguments, environment, timeout))
        if phase in self.results and self.results[phase]:
            result = self.results[phase].pop(0)
            if check and result.returncode:
                raise SmokeError("phase_failed", f"{phase} failed")
            return result
        if phase == self.fail_phase:
            if check:
                raise SmokeError("phase_failed", f"{phase} failed")
            return CommandResult(1, stderr=b"ordinary build failure")
        if phase in self.overrides:
            return CommandResult(0, self.overrides[phase])
        payloads: dict[str, object] = {
            "data": {
                "data_version": DATA_VERSION,
                "manifest_checksum": CHECKSUM,
                "path": f"/artifacts/processed/{DATA_VERSION}",
            },
            "train": {
                "model_version": MODEL_VERSION,
                "manifest_checksum": MODEL_CHECKSUM,
                "bundle_checksum": "c" * 64,
                "bundle_path": f"/artifacts/models/{MODEL_VERSION}/bundle.json",
                "status": "READY",
            },
            "api_health": {"status": "ok", "service": "api"},
            "register": {
                "model_version": MODEL_VERSION,
                "data_version": DATA_VERSION,
                "data_manifest_checksum": CHECKSUM,
                "manifest_checksum": MODEL_CHECKSUM,
                "artifact_checksum": "c" * 64,
                "status": "READY",
            },
            "activate": {"model_version": MODEL_VERSION, "status": "ACTIVE"},
            "api_ready": {"status": "ready", "checks": {"active_model_restore": "restored"}},
            "search_reindex": {
                "status": "ok",
                "physical_index": "microlens-items-smoke-checkpoint6-a",
            },
            "final_ready": {
                "status": "ready",
                "checks": {"active_model_restore": "restored"},
            },
            "final_openapi": {
                "openapi": "3.1.0",
                "paths": {"/api/feeds/{feed_type}": {}},
            },
            "final_web": {"status": 200, "body_bytes": 128},
        }
        if phase == "services":
            return CommandResult(0, ("\n".join(sorted(EXPECTED_SERVICES)) + "\n").encode())
        if phase in payloads:
            return CommandResult(0, json.dumps(payloads[phase]).encode())
        return CommandResult(0)


def workspace(root: Path) -> None:
    (root / "scripts").mkdir()
    (root / "dataset").mkdir()
    env_path = root / ".env"
    env_path.write_text("COMPOSE_PROJECT_NAME=microlens-review\n", encoding="utf-8")
    env_path.chmod(0o600)
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "scripts/compose.integration.yaml").write_text("services: {}\n", encoding="utf-8")
    (root / "scripts/compose.smoke.yaml").write_text("services: {}\n", encoding="utf-8")
    for name in (
        "MicroLens-50k_pairs.csv",
        "MicroLens-50k_titles.csv",
        "MicroLens-50k_likes_and_views.txt",
    ):
        (root / "dataset" / name).write_text("fixture\n", encoding="utf-8")


class SmokeAllTests(unittest.TestCase):
    def test_happy_path_has_exact_isolation_and_frozen_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner()
            result = SmokeOrchestrator(
                SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
            ).run()

        self.assertEqual(result.model_version, MODEL_VERSION)
        self.assertEqual(result.final_services, tuple(sorted(EXPECTED_SERVICES)))
        self.assertEqual(result.as_dict()["final_services"], sorted(EXPECTED_SERVICES))
        phases = [call[0] for call in runner.calls]
        ordered = [
            "build",
            "data",
            "train",
            "core_up",
            "api_health",
            "migrate",
            "seed",
            "register",
            "activate",
            "api_restore",
            "api_ready",
            "search_reindex",
            "final_up",
            "final_ready",
            "final_openapi",
            "final_web",
            "services",
        ]
        positions = [phases.index(phase) for phase in ordered]
        self.assertEqual(positions, sorted(positions))

        compose_prefix = [
            "docker",
            "compose",
            "--project-name",
            "microlens-review",
            "--env-file",
            ".env",
        ]
        for compose_file in COMPOSE_FILES:
            compose_prefix.extend(("-f", compose_file))
        compose_calls = [
            call
            for call in runner.calls
            if call[1][:2] == ["docker", "compose"] and call[0] != "compose_version"
        ]
        self.assertTrue(compose_calls)
        for _phase, command, environment, _timeout in compose_calls:
            self.assertEqual(command[: len(compose_prefix)], compose_prefix)
            self.assertEqual(environment["COMPOSE_PROJECT_NAME"], "microlens-review")
            self.assertEqual(environment["PROCESSED_DATA_DIR"], "./artifacts/data")
            self.assertEqual(environment["API_PORT"], "18080")
            self.assertEqual(environment["WEB_PORT"], "25173")
            self.assertEqual(environment["PHASE2D_POSTGRES_PORT"], "45432")
            self.assertEqual(environment["PHASE2D_REDIS_PORT"], "46379")

        flattened = " ".join(
            token.lower() for _, command, _, _ in runner.calls for token in command
        )
        for forbidden in ("flushall", "truncate", " reset ", " down ", "volume rm"):
            self.assertNotIn(forbidden, f" {flattened} ")

    def test_failure_stops_before_any_later_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner(fail_phase="train")
            with self.assertRaises(SmokeError):
                SmokeOrchestrator(
                    SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
                ).run()
        phases = [call[0] for call in runner.calls]
        self.assertIn("train", phases)
        self.assertNotIn("core_up", phases)

    def test_exact_buildx_session_error_uses_reviewed_direct_build_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner()
            runner.overrides["build"] = b""
            original_run = runner.run

            def run_with_buildx_failure(*args: object, **kwargs: object) -> CommandResult:
                if kwargs["phase"] == "build":
                    runner.calls.append(
                        (
                            str(kwargs["phase"]),
                            list(args[0]),
                            dict(kwargs["environment"]),
                            int(kwargs["timeout"]),
                        )
                    )
                    return CommandResult(1, stderr=BUILDX_SESSION_ERROR)
                return original_run(*args, **kwargs)

            runner.run = run_with_buildx_failure  # type: ignore[method-assign]
            SmokeOrchestrator(
                SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
            ).run()

        phases = [call[0] for call in runner.calls]
        self.assertEqual(
            [phase for phase in phases if phase.startswith("build_fallback_")],
            [
                "build_fallback_api",
                "build_fallback_worker",
                "build_fallback_web",
                "build_fallback_scheduler",
                "build_fallback_smoke-bootstrap",
            ],
        )

    def test_unrecognized_compose_build_failure_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner(fail_phase="build")
            with self.assertRaisesRegex(SmokeError, "build failed"):
                SmokeOrchestrator(
                    SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
                ).run()
        phases = [call[0] for call in runner.calls]
        self.assertNotIn("build_fallback_api", phases)
        self.assertNotIn("data", phases)

    def test_exact_compose_reconciliation_failure_retries_to_bounded_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner()
            runner.results["data"] = [
                CommandResult(1, stderr=COMPOSE_RECONCILIATION_ERRORS[0]),
                CommandResult(1, stderr=COMPOSE_RECONCILIATION_ERRORS[1]),
                CommandResult(1, stderr=COMPOSE_RECONCILIATION_ERRORS[0]),
                CommandResult(1, stderr=COMPOSE_RECONCILIATION_ERRORS[1]),
                CommandResult(
                    0,
                    json.dumps(
                        {
                            "data_version": DATA_VERSION,
                            "manifest_checksum": CHECKSUM,
                            "path": f"/artifacts/processed/{DATA_VERSION}",
                        }
                    ).encode(),
                ),
            ]
            SmokeOrchestrator(
                SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
            ).run()

        self.assertEqual(
            [phase for phase, *_ in runner.calls].count("data"),
            COMPOSE_RECONCILIATION_MAX_ATTEMPTS,
        )

    def test_unrecognized_failure_during_reconciliation_retry_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner()
            runner.results["data"] = [
                CommandResult(1, stderr=COMPOSE_RECONCILIATION_ERRORS[0]),
                CommandResult(1, stderr=b"unknown retry failure"),
            ]
            with self.assertRaisesRegex(SmokeError, "data failed"):
                SmokeOrchestrator(
                    SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
                ).run()

        self.assertEqual([phase for phase, *_ in runner.calls].count("data"), 2)
        self.assertNotIn("train", [phase for phase, *_ in runner.calls])

    def test_unrecognized_data_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner()
            runner.results["data"] = [CommandResult(1, stderr=b"ordinary data failure")]
            with self.assertRaisesRegex(SmokeError, "data failed"):
                SmokeOrchestrator(
                    SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
                ).run()

        self.assertEqual([phase for phase, *_ in runner.calls].count("data"), 1)
        self.assertNotIn("train", [phase for phase, *_ in runner.calls])

    def test_existing_run_resource_is_rejected_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner()
            dirty_phase = "resource_volume_microlens-review-smoke-checkpoint6-a-postgres"
            runner.overrides[dirty_phase] = b"microlens-review-smoke-checkpoint6-a-postgres\n"
            with self.assertRaisesRegex(SmokeError, "choose a new run ID"):
                SmokeOrchestrator(
                    SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
                ).run()
        self.assertNotIn("build", [call[0] for call in runner.calls])

    def test_group_readable_env_is_rejected_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            (root / ".env").chmod(0o640)
            runner = FakeRunner()
            with self.assertRaisesRegex(SmokeError, "group/other"):
                SmokeOrchestrator(
                    SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
                ).run()
        self.assertEqual(runner.calls, [])

    def test_non_json_or_mismatched_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            runner = FakeRunner()
            runner.overrides["data"] = b'{}\n{"second":true}\n'
            with self.assertRaisesRegex(SmokeError, "one JSON document"):
                SmokeOrchestrator(
                    SmokeSpec.create("checkpoint6-a", "docker compose"), runner, repo_root=root
                ).run()

    def test_run_id_and_compose_command_are_fail_closed(self) -> None:
        for value in ("ab", "../escape", "default", "UPPER"):
            with self.subTest(run_id=value), self.assertRaises(SmokeError):
                SmokeSpec.create(value, "docker compose")
        for value in ("docker-compose", "sudo docker compose", "docker compose --ansi never"):
            with self.subTest(command=value), self.assertRaises(SmokeError):
                SmokeSpec.create("checkpoint6-a", value)

    def test_smoke_override_is_labeled_isolated_and_non_destructive(self) -> None:
        root = Path(__file__).resolve().parents[2]
        document = yaml.safe_load((root / "scripts/compose.smoke.yaml").read_text())
        self.assertEqual(
            document["services"]["api"]["environment"]["API_BOOTSTRAP_MODE"], "external"
        )
        self.assertIn("/health", " ".join(document["services"]["api"]["healthcheck"]["test"]))
        scheduler_health = document["services"]["scheduler"]["healthcheck"]["test"]
        self.assertEqual(scheduler_health[-1], "--healthcheck")
        self.assertIn(":ro", document["services"]["smoke-bootstrap"]["volumes"][0])
        for resource in (*document["volumes"].values(), *document["networks"].values()):
            self.assertEqual(resource["labels"]["io.microlens.environment_kind"], "phase6_smoke")
        source = (root / "scripts/smoke_all.py").read_text().lower()
        for forbidden in ('"down"', '"rm"', "flushall", "truncate", '"reset"'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
