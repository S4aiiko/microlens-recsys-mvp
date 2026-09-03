from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT = "microlens-review"
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMPOSE_FILES = (
    "compose.yaml",
    "scripts/compose.integration.yaml",
    "scripts/compose.smoke.yaml",
)
REQUIRED_INPUTS = (
    "MicroLens-50k_pairs.csv",
    "MicroLens-50k_titles.csv",
    "MicroLens-50k_likes_and_views.txt",
)
EXPECTED_SERVICES = {"api", "db", "redis", "scheduler", "search", "web", "worker"}
BUILDX_SESSION_ERROR = (
    b'header key "x-docker-expose-session-sharedkey" contains value with non-printable ASCII'
)
COMPOSE_RECONCILIATION_ERRORS = (
    b"does not match the compose file",
    b"is not connected to the network microlens-review_edge",
)
# Docker Desktop 4.88.1 / Compose 5.4.0 may need several convergence cycles
# when replacing a prior run's labeled containers and networks.
COMPOSE_RECONCILIATION_MAX_ATTEMPTS = 5
ACTIVATE_SCRIPT = """\
import json, os, sys, urllib.request
payload = json.dumps({"expected_current_version": None, "manifest_checksum": sys.argv[2]}).encode()
request = urllib.request.Request(
    f"http://127.0.0.1:8001/internal/model-versions/{sys.argv[1]}/activate",
    data=payload,
    headers={"Content-Type": "application/json", "X-Publish-Token": os.environ["PUBLISH_TOKEN"]},
)
with urllib.request.urlopen(request, timeout=20) as response:
    print(response.read().decode("utf-8"))
"""
HTTP_JSON_SCRIPT = """\
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=20) as response:
    print(response.read().decode("utf-8"))
"""
HTTP_WEB_SCRIPT = """\
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=20) as response:
    body = response.read()
    print(json.dumps({"status": response.status, "body_bytes": len(body)}))
"""


class SmokeError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class Runner:
    def run(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str],
        timeout: int,
        phase: str,
        check: bool = True,
    ) -> CommandResult:
        merged = os.environ.copy()
        merged.update(environment)
        try:
            completed = subprocess.run(
                arguments,
                cwd=Path(__file__).resolve().parents[1],
                env=merged,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SmokeError("command_unavailable", f"{phase} could not complete") from exc
        result = CommandResult(completed.returncode, completed.stdout, completed.stderr)
        if check and result.returncode != 0:
            raise SmokeError("phase_failed", f"{phase} failed with exit {result.returncode}")
        return result


@dataclass(frozen=True, slots=True)
class SmokeSpec:
    run_id: str
    docker_compose: tuple[str, str]

    @classmethod
    def create(cls, run_id: str, docker_compose: str) -> SmokeSpec:
        if not RUN_ID_RE.fullmatch(run_id) or run_id in {"default", "development", "production"}:
            raise SmokeError("invalid_run_id", "SMOKE_RUN_ID must be a non-default safe identifier")
        try:
            command = tuple(shlex.split(docker_compose))
        except ValueError as exc:
            raise SmokeError("invalid_compose_command", "DOCKER_COMPOSE is invalid") from exc
        if command != ("docker", "compose"):
            raise SmokeError(
                "invalid_compose_command", "Phase 6 smoke requires the exact docker compose command"
            )
        return cls(run_id=run_id, docker_compose=(command[0], command[1]))

    @property
    def environment(self) -> dict[str, str]:
        return {
            "API_PORT": "18080",
            "COMPOSE_PROJECT_NAME": PROJECT,
            "MICROLENS_DATA_DIR": "./dataset",
            "PHASE2D_POSTGRES_PORT": "45432",
            "PHASE2D_REDIS_PORT": "46379",
            "PROCESSED_DATA_DIR": "./artifacts/data",
            "SMOKE_API_RESTORE_ACTIVE_MODEL": "false",
            "SMOKE_RUN_ID": self.run_id,
            "WEB_ORIGIN": "http://localhost:25173",
            "WEB_PORT": "25173",
        }

    @property
    def compose(self) -> list[str]:
        command = [*self.docker_compose, "--project-name", PROJECT, "--env-file", ".env"]
        for path in COMPOSE_FILES:
            command.extend(("-f", path))
        return command

    @property
    def volumes(self) -> tuple[str, ...]:
        prefix = f"{PROJECT}-smoke-{self.run_id}"
        return tuple(
            f"{prefix}-{suffix}"
            for suffix in (
                "postgres",
                "redis",
                "search",
                "models",
                "training-exports",
                "analytics-exports",
            )
        )

    @property
    def networks(self) -> tuple[str, ...]:
        prefix = f"{PROJECT}-smoke-{self.run_id}"
        return (f"{prefix}-edge", f"{prefix}-backend")


@dataclass(frozen=True, slots=True)
class SmokeResult:
    run_id: str
    data_version: str
    data_manifest_checksum: str
    model_version: str
    model_manifest_checksum: str
    model_bundle_checksum: str
    search_index: str
    final_services: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "PASS",
            "project": PROJECT,
            "run_id": self.run_id,
            "data_version": self.data_version,
            "data_manifest_checksum": self.data_manifest_checksum,
            "model_version": self.model_version,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_bundle_checksum": self.model_bundle_checksum,
            "search_index": self.search_index,
            "final_services": list(self.final_services),
        }


class SmokeOrchestrator:
    def __init__(self, spec: SmokeSpec, runner: Runner, *, repo_root: Path | None = None) -> None:
        self.spec = spec
        self.runner = runner
        self.repo_root = repo_root or Path(__file__).resolve().parents[1]
        self.environment = spec.environment

    def run(self) -> SmokeResult:
        self._validate_workspace()
        self._preflight()
        self._build_images()
        data = self._json_command(
            "data",
            [
                *self.spec.compose,
                "run",
                "--no-deps",
                "smoke-bootstrap",
                "python",
                "-m",
                "recsys.data.cli",
                "build-official",
                "--config",
                "/workspace/configs/data/smoke.yaml",
                "--raw-dir",
                "/input/microlens",
                "--output-root",
                "/artifacts/processed",
            ],
            timeout=900,
            retry_compose_reconciliation=True,
        )
        data_version = self._string(data, "data_version")
        data_checksum = self._checksum(data, "manifest_checksum")
        if data.get("path") != f"/artifacts/processed/{data_version}":
            raise SmokeError("unexpected_data_path", "data CLI returned an unexpected path")
        model = self._json_command(
            "train",
            [
                *self.spec.compose,
                "run",
                "--no-deps",
                "smoke-bootstrap",
                "python",
                "-m",
                "recsys.cli.train_model",
                "--processed-root",
                "/artifacts/processed",
                "--data-version",
                data_version,
                "--data-manifest-checksum",
                data_checksum,
                "--config",
                "/workspace/configs/models/smoke-a.json",
                "--output-root",
                "/artifacts/models",
            ],
            timeout=3600,
        )
        model_version = self._string(model, "model_version")
        model_checksum = self._checksum(model, "manifest_checksum")
        bundle_checksum = self._checksum(model, "bundle_checksum")
        if model.get("status") != "READY":
            raise SmokeError("model_not_ready", "training did not produce a READY model")
        if model.get("bundle_path") != f"/artifacts/models/{model_version}/bundle.json":
            raise SmokeError("unexpected_model_path", "training returned an unexpected bundle path")

        self._command(
            "core_up",
            [*self.spec.compose, "up", "-d", "--wait", "db", "redis", "search", "api"],
            timeout=900,
            retry_compose_reconciliation=True,
        )
        health = self._json_command(
            "api_health",
            [sys.executable, "-c", HTTP_JSON_SCRIPT, "http://localhost:18080/health"],
        )
        if health.get("status") != "ok" or health.get("service") != "api":
            raise SmokeError("api_health_failed", "API health payload is invalid")
        self._command(
            "migrate",
            [
                *self.spec.compose,
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "scripts.platform_commands",
                "migrate",
            ],
            timeout=300,
        )
        self._command(
            "seed",
            [
                *self.spec.compose,
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "scripts.platform_commands",
                "seed",
            ],
            timeout=300,
        )
        registration = self._json_command(
            "register",
            [
                *self.spec.compose,
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "apps.api.app.cli.register_model",
                "--artifact-uri",
                f"{model_version}/bundle.json",
                "--manifest-checksum",
                model_checksum,
            ],
            timeout=300,
        )
        if (
            registration.get("model_version") != model_version
            or registration.get("data_version") != data_version
            or registration.get("data_manifest_checksum") != data_checksum
            or registration.get("manifest_checksum") != model_checksum
            or registration.get("artifact_checksum") != bundle_checksum
            or registration.get("status") != "READY"
        ):
            raise SmokeError("registration_mismatch", "registered model identity does not match")
        activation = self._json_command(
            "activate",
            [
                *self.spec.compose,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                ACTIVATE_SCRIPT,
                model_version,
                model_checksum,
            ],
            timeout=300,
        )
        if activation.get("model_version") != model_version or activation.get("status") != "ACTIVE":
            raise SmokeError("activation_mismatch", "activation response does not match")

        restored_environment = {**self.environment, "SMOKE_API_RESTORE_ACTIVE_MODEL": "true"}
        self._command(
            "api_restore",
            [*self.spec.compose, "up", "-d", "--no-deps", "--force-recreate", "--wait", "api"],
            timeout=600,
            environment=restored_environment,
        )
        ready = self._json_command(
            "api_ready",
            [sys.executable, "-c", HTTP_JSON_SCRIPT, "http://localhost:18080/ready"],
            environment=restored_environment,
        )
        checks = ready.get("checks")
        if (
            ready.get("status") != "ready"
            or not isinstance(checks, dict)
            or checks.get("active_model_restore") != "restored"
        ):
            raise SmokeError("active_restore_failed", "API did not restore the activated model")

        index_version = f"smoke-{self.spec.run_id}"
        physical_index = f"microlens-items-{index_version}"
        search = self._json_command(
            "search_reindex",
            [
                *self.spec.compose,
                "run",
                "--no-deps",
                "smoke-bootstrap",
                "python",
                "-m",
                "apps.api.app.cli.search_reindex",
                "--index-version",
                index_version,
                "--source-version",
                data_version,
            ],
            timeout=900,
            environment=restored_environment,
        )
        if search.get("status") != "ok" or search.get("physical_index") != physical_index:
            raise SmokeError("search_reindex_failed", "search index identity does not match")

        self._command(
            "final_up",
            [*self.spec.compose, "up", "-d", "--wait", "scheduler", "worker", "web"],
            timeout=900,
            environment=restored_environment,
        )
        final_ready = self._json_command(
            "final_ready",
            [sys.executable, "-c", HTTP_JSON_SCRIPT, "http://localhost:18080/ready"],
            environment=restored_environment,
        )
        if final_ready.get("status") != "ready":
            raise SmokeError("final_api_not_ready", "final API readiness failed")
        openapi = self._json_command(
            "final_openapi",
            [sys.executable, "-c", HTTP_JSON_SCRIPT, "http://localhost:18080/openapi.json"],
            environment=restored_environment,
        )
        if openapi.get("openapi") != "3.1.0" or "/api/feeds/{feed_type}" not in openapi.get(
            "paths", {}
        ):
            raise SmokeError("openapi_invalid", "runtime public OpenAPI is incomplete")
        web = self._json_command(
            "final_web",
            [sys.executable, "-c", HTTP_WEB_SCRIPT, "http://localhost:25173/"],
            environment=restored_environment,
        )
        if (
            web.get("status") != 200
            or not isinstance(web.get("body_bytes"), int)
            or not web["body_bytes"]
        ):
            raise SmokeError("web_health_failed", "Web root is not reachable")
        services = (
            self._command(
                "services",
                [*self.spec.compose, "ps", "--services", "--status", "running"],
                environment=restored_environment,
            )
            .stdout.decode("utf-8", "strict")
            .splitlines()
        )
        if set(services) != EXPECTED_SERVICES or len(services) != len(EXPECTED_SERVICES):
            raise SmokeError("service_set_mismatch", "not all required services are running")
        return SmokeResult(
            run_id=self.spec.run_id,
            data_version=data_version,
            data_manifest_checksum=data_checksum,
            model_version=model_version,
            model_manifest_checksum=model_checksum,
            model_bundle_checksum=bundle_checksum,
            search_index=physical_index,
            final_services=tuple(sorted(services)),
        )

    def _build_images(self) -> None:
        result = self._command(
            "build",
            [
                *self.spec.compose,
                "build",
                "--pull",
                "--no-cache",
                "api",
                "worker",
                "scheduler",
                "web",
                "smoke-bootstrap",
            ],
            timeout=3600,
            check=False,
        )
        if result.returncode == 0:
            return
        if BUILDX_SESSION_ERROR not in result.stdout + result.stderr:
            raise SmokeError("phase_failed", f"build failed with exit {result.returncode}")

        builds = (
            ("api", "microlens-review-api", "apps/api/Dockerfile"),
            ("worker", "microlens-review-worker", "apps/worker/Dockerfile"),
            ("web", "microlens-review-web", "apps/web/Dockerfile"),
        )
        for service, image, dockerfile in builds:
            self._command(
                f"build_fallback_{service}",
                [
                    "docker",
                    "build",
                    "--pull",
                    "--no-cache",
                    "--tag",
                    image,
                    "--file",
                    dockerfile,
                    ".",
                ],
                timeout=3600,
            )
        for service in ("scheduler", "smoke-bootstrap"):
            self._command(
                f"build_fallback_{service}",
                ["docker", "tag", "microlens-review-worker", f"microlens-review-{service}"],
            )

    def _validate_workspace(self) -> None:
        required = [self.repo_root / ".env", *(self.repo_root / path for path in COMPOSE_FILES)]
        for path in required:
            if path.is_symlink() or not path.is_file():
                raise SmokeError(
                    "workspace_input_missing", f"required file is missing: {path.name}"
                )
        env_mode = stat.S_IMODE((self.repo_root / ".env").stat().st_mode)
        if env_mode & 0o077:
            raise SmokeError("env_permissions_unsafe", ".env must not be accessible by group/other")
        raw = self.repo_root / "dataset"
        if raw.is_symlink() or not raw.is_dir():
            raise SmokeError("dataset_missing", "dataset must be a real directory")
        for name in REQUIRED_INPUTS:
            path = raw / name
            if path.is_symlink() or not path.is_file():
                raise SmokeError(
                    "dataset_input_missing", f"required dataset file is missing: {name}"
                )
        artifacts = self.repo_root / "artifacts"
        if artifacts.exists() and (artifacts.is_symlink() or not artifacts.is_dir()):
            raise SmokeError("processed_path_unsafe", "artifacts must be a real directory")
        artifacts.mkdir(exist_ok=True)
        processed = artifacts / "data"
        if processed.exists() and (processed.is_symlink() or not processed.is_dir()):
            raise SmokeError(
                "processed_path_unsafe", "processed data path must be a real directory"
            )
        processed.mkdir(exist_ok=True)
        if processed.is_symlink() or not processed.is_dir():
            raise SmokeError(
                "processed_path_unsafe", "processed data path must be a real directory"
            )

    def _preflight(self) -> None:
        self._command("docker_version", ["docker", "version"], timeout=30)
        self._command("compose_version", ["docker", "compose", "version"], timeout=30)
        self._command("docker_daemon", ["docker", "info", "--format", "{{.ServerVersion}}"])
        self._command("compose_config", [*self.spec.compose, "config", "--quiet"])
        for kind, names in (("volume", self.spec.volumes), ("network", self.spec.networks)):
            for name in names:
                result = self._command(
                    f"resource_{kind}_{name}",
                    ["docker", kind, "ls", "--quiet", "--filter", f"name=^{name}$"],
                )
                observed = [
                    line for line in result.stdout.decode("utf-8", "strict").splitlines() if line
                ]
                if observed:
                    raise SmokeError(
                        "dirty_smoke_resource", f"smoke {kind} already exists; choose a new run ID"
                    )

    def _command(
        self,
        phase: str,
        arguments: list[str],
        *,
        timeout: int = 60,
        environment: dict[str, str] | None = None,
        check: bool = True,
        retry_compose_reconciliation: bool = False,
    ) -> CommandResult:
        if not retry_compose_reconciliation:
            return self.runner.run(
                arguments,
                environment=environment or self.environment,
                timeout=timeout,
                phase=phase,
                check=check,
            )
        for attempt in range(COMPOSE_RECONCILIATION_MAX_ATTEMPTS):
            result = self.runner.run(
                arguments,
                environment=environment or self.environment,
                timeout=timeout,
                phase=phase,
                check=False,
            )
            if result.returncode == 0:
                return result
            diagnostic = result.stdout + result.stderr
            recognized = any(marker in diagnostic for marker in COMPOSE_RECONCILIATION_ERRORS)
            if not recognized or attempt == COMPOSE_RECONCILIATION_MAX_ATTEMPTS - 1:
                if check:
                    raise SmokeError(
                        "phase_failed", f"{phase} failed with exit {result.returncode}"
                    )
                return result
        raise AssertionError("reconciliation loop must return or raise")

    def _json_command(
        self,
        phase: str,
        arguments: list[str],
        *,
        timeout: int = 60,
        environment: dict[str, str] | None = None,
        retry_compose_reconciliation: bool = False,
    ) -> dict[str, Any]:
        result = self._command(
            phase,
            arguments,
            timeout=timeout,
            environment=environment,
            retry_compose_reconciliation=retry_compose_reconciliation,
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeError(
                "invalid_phase_json", f"{phase} did not return one JSON document"
            ) from exc
        if not isinstance(payload, dict):
            raise SmokeError("invalid_phase_json", f"{phase} JSON must be an object")
        return payload

    @staticmethod
    def _string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise SmokeError("invalid_phase_json", f"required field is invalid: {field}")
        return value

    @staticmethod
    def _checksum(payload: dict[str, Any], field: str) -> str:
        value = SmokeOrchestrator._string(payload, field)
        if not SHA256_RE.fullmatch(value):
            raise SmokeError("invalid_phase_json", f"required checksum is invalid: {field}")
        return value


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Fail-closed Phase 6 Compose smoke orchestrator")
    command.add_argument("--run-id", required=True)
    command.add_argument("--docker-compose", default="docker compose")
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        spec = SmokeSpec.create(arguments.run_id, arguments.docker_compose)
        result = SmokeOrchestrator(spec, Runner()).run()
    except SmokeError as exc:
        print(json.dumps({"status": "BLOCKED", "code": exc.code, "message": str(exc)}))
        return exc.exit_code
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
