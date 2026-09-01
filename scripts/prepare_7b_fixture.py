from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, NoReturn

FIXTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
MODEL_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
DEFAULT_PROJECT = "microlens-mvp"
PROJECT_PREFIX = "microlens-7b-"
LABEL_KIND = "io.microlens.environment_kind"
LABEL_ID = "io.microlens.fixture_id"
FIXTURE_KIND = "7b_fixture"
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
FIXED_GRAPH_NAMESPACE = uuid.UUID("149a4cea-5e61-55db-8fac-cfeb1df153a5")
BLOCKED_INPUT = 20
INVALID_INPUT = 21
DIRTY_FIXTURE = 22
SAFETY_CHECK_FAILED = 23


class FixtureError(RuntimeError):
    def __init__(self, status: str, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class BundleEvidence:
    path: Path
    model_version: str
    data_version: str
    artifact_checksum: str
    manifest_checksum: str
    config_checksum: str
    evidence_kind: str
    validated_payload: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    project: str
    db_port: int
    redis_port: int
    api_port: int
    web_origin_port: int
    artifact_namespace: str

    @classmethod
    def create(cls, fixture_id: str) -> FixtureSpec:
        if not FIXTURE_ID_RE.fullmatch(fixture_id):
            raise FixtureError(
                "INVALID_INPUT",
                "invalid_fixture_id",
                "FIXTURE_ID must match ^[a-z0-9][a-z0-9-]{2,31}$",
                INVALID_INPUT,
            )
        if fixture_id in {"default", "development", "microlens", "microlens-mvp"}:
            raise FixtureError(
                "INVALID_INPUT",
                "reserved_fixture_id",
                "FIXTURE_ID names a reserved/default environment",
                INVALID_INPUT,
            )
        project = f"{PROJECT_PREFIX}{fixture_id}"
        if project == DEFAULT_PROJECT or not project.startswith(PROJECT_PREFIX):
            raise FixtureError(
                "INVALID_INPUT",
                "default_project_forbidden",
                "fixture project must be isolated from the default project",
                INVALID_INPUT,
            )
        slot = int(hashlib.sha256(fixture_id.encode()).hexdigest()[:8], 16) % 4_000
        return cls(
            fixture_id=fixture_id,
            project=project,
            db_port=20_000 + slot,
            redis_port=25_000 + slot,
            api_port=30_000 + slot,
            web_origin_port=35_000 + slot,
            artifact_namespace=f"/artifacts/experiments/7b/{fixture_id}",
        )

    def environment(self, credentials: dict[str, str]) -> dict[str, str]:
        return {
            "FIXTURE_ID": self.fixture_id,
            "FIXTURE_DB_PORT": str(self.db_port),
            "FIXTURE_REDIS_PORT": str(self.redis_port),
            "FIXTURE_API_PORT": str(self.api_port),
            "FIXTURE_WEB_ORIGIN_PORT": str(self.web_origin_port),
            "FIXTURE_POSTGRES_PASSWORD": credentials["FIXTURE_POSTGRES_PASSWORD"],
            "FIXTURE_JWT_SECRET": credentials["FIXTURE_JWT_SECRET"],
            "FIXTURE_PUBLISH_TOKEN": credentials["FIXTURE_PUBLISH_TOKEN"],
            "FIXTURE_SEED_PASSWORD": credentials["FIXTURE_SEED_PASSWORD"],
        }


class Runner:
    def run(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        input_payload: bytes | None = None,
        check: bool = True,
    ) -> bytes:
        merged = os.environ.copy()
        if environment:
            merged.update(environment)
        completed = subprocess.run(
            arguments,
            env=merged,
            input=input_payload,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            suffix = " | ".join(detail[-6:]) if detail else f"exit {completed.returncode}"
            operation = Path(arguments[0]).name
            if len(arguments) > 1:
                operation += f" {arguments[1]}"
            raise FixtureError(
                "SAFETY_CHECK_FAILED",
                "command_failed",
                f"readiness/safety command failed during {operation}: {suffix}",
                SAFETY_CHECK_FAILED,
            )
        return completed.stdout


def _fail(status: str, code: str, message: str, exit_code: int) -> NoReturn:
    raise FixtureError(status, code, message, exit_code)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_root_directory(repo_root: Path, *, code: str, exit_code: int) -> int:
    try:
        before = repo_root.lstat()
    except FileNotFoundError:
        _fail("INVALID_INPUT", code, "repository root does not exist", exit_code)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        _fail("INVALID_INPUT", code, "repository root must be a real directory", exit_code)
    try:
        descriptor = os.open(repo_root, _directory_flags())
    except OSError:
        _fail("INVALID_INPUT", code, "repository root could not be opened safely", exit_code)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(descriptor)
        _fail("INVALID_INPUT", code, "repository root changed during validation", exit_code)
    return descriptor


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    missing_status: str,
    missing_code: str,
    missing_message: str,
    missing_exit_code: int,
    unsafe_code: str,
    unsafe_message: str,
    unsafe_exit_code: int,
) -> int:
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            _fail(missing_status, missing_code, missing_message, missing_exit_code)
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            # A concurrent creator/replacer won the race. Validate its exact object below.
            pass
        except OSError:
            _fail("INVALID_INPUT", unsafe_code, unsafe_message, unsafe_exit_code)
        try:
            before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError:
            _fail("INVALID_INPUT", unsafe_code, unsafe_message, unsafe_exit_code)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        _fail("INVALID_INPUT", unsafe_code, unsafe_message, unsafe_exit_code)
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError:
        _fail("INVALID_INPUT", unsafe_code, unsafe_message, unsafe_exit_code)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(descriptor)
        _fail("INVALID_INPUT", unsafe_code, unsafe_message, unsafe_exit_code)
    return descriptor


def _safe_existing_file(raw: str, *, repo_root: Path) -> tuple[Path, bytes]:
    if not raw:
        _fail(
            "BLOCKED_INPUT",
            "phase3_bundle_missing",
            "two Phase 3 ModelBundle paths and their expected SHA-256 values are required",
            BLOCKED_INPUT,
        )
    lexical = Path(raw)
    if any(part in {"", ".", ".."} for part in PurePath(raw).parts):
        _fail(
            "INVALID_INPUT",
            "unsafe_bundle_path",
            "bundle path cannot contain traversal or ambiguous components",
            INVALID_INPUT,
        )
    artifact_root = repo_root / "artifacts" / "models"
    candidate = lexical if lexical.is_absolute() else repo_root / lexical
    candidate = candidate.absolute()
    try:
        candidate.relative_to(artifact_root.absolute())
    except ValueError:
        _fail(
            "INVALID_INPUT",
            "bundle_outside_model_artifacts",
            "bundle must be below the repository artifacts/models directory",
            INVALID_INPUT,
        )
    relative = candidate.relative_to(artifact_root.absolute())
    if not relative.parts:
        _fail(
            "INVALID_INPUT",
            "bundle_not_regular",
            "bundle path must name a regular file below artifacts/models",
            INVALID_INPUT,
        )
    root_descriptor = _open_root_directory(
        repo_root,
        code="symlink_bundle_forbidden",
        exit_code=INVALID_INPUT,
    )
    directory_descriptors = [root_descriptor]
    try:
        for component in ("artifacts", "models", *relative.parts[:-1]):
            child = _open_child_directory(
                directory_descriptors[-1],
                component,
                create=False,
                missing_status="BLOCKED_INPUT",
                missing_code="phase3_bundle_missing",
                missing_message="a required Phase 3 ModelBundle directory does not exist",
                missing_exit_code=BLOCKED_INPUT,
                unsafe_code="symlink_bundle_forbidden",
                unsafe_message="bundle path and ancestors must be real directories",
                unsafe_exit_code=INVALID_INPUT,
            )
            directory_descriptors.append(child)
        filename = relative.parts[-1]
        try:
            before = os.stat(
                filename,
                dir_fd=directory_descriptors[-1],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            _fail(
                "BLOCKED_INPUT",
                "phase3_bundle_missing",
                "a required Phase 3 ModelBundle does not exist",
                BLOCKED_INPUT,
            )
        except OSError:
            _fail(
                "INVALID_INPUT",
                "bundle_not_regular",
                "bundle metadata could not be read safely",
                INVALID_INPUT,
            )
        if stat.S_ISLNK(before.st_mode):
            _fail(
                "INVALID_INPUT",
                "symlink_bundle_forbidden",
                "bundle path and ancestors must not be symlinks",
                INVALID_INPUT,
            )
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptors[-1],
            )
        except OSError:
            _fail(
                "INVALID_INPUT",
                "symlink_bundle_forbidden",
                "bundle could not be opened without following links",
                INVALID_INPUT,
            )
        metadata = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail(
                "INVALID_INPUT",
                "bundle_changed_during_read",
                "bundle changed before validation",
                INVALID_INPUT,
            )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            _fail(
                "INVALID_INPUT",
                "bundle_not_regular",
                "bundle must be a non-empty regular file",
                INVALID_INPUT,
            )
        if metadata.st_size > MAX_BUNDLE_BYTES:
            _fail(
                "INVALID_INPUT",
                "bundle_too_large",
                "bundle exceeds the 16 MiB staging limit",
                INVALID_INPUT,
            )
        payload = bytearray()
        while len(payload) <= MAX_BUNDLE_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_BUNDLE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _fail(
                "INVALID_INPUT",
                "bundle_changed_during_read",
                "bundle changed during validation",
                INVALID_INPUT,
            )
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
    return candidate, bytes(payload)


def validate_bundle(
    raw_path: str,
    expected_checksum: str,
    *,
    repo_root: Path,
    protocol_test_only: bool,
) -> BundleEvidence:
    if not SHA256_RE.fullmatch(expected_checksum):
        _fail(
            "BLOCKED_INPUT",
            "bundle_checksum_missing",
            "each ModelBundle requires an explicit lowercase SHA-256",
            BLOCKED_INPUT,
        )
    path, payload = _safe_existing_file(raw_path, repo_root=repo_root)
    actual = _sha256(payload)
    if actual != expected_checksum:
        _fail(
            "BLOCKED_INPUT",
            "bundle_checksum_mismatch",
            "a ModelBundle does not match its explicit expected SHA-256",
            BLOCKED_INPUT,
        )
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(
            "BLOCKED_INPUT",
            "bundle_not_loadable",
            "ModelBundle must be a loadable JSON object",
            BLOCKED_INPUT,
        )
    if not isinstance(document, dict):
        _fail(
            "BLOCKED_INPUT",
            "bundle_not_loadable",
            "ModelBundle must be a loadable JSON object",
            BLOCKED_INPUT,
        )
    model_version = document.get("model_version")
    data_version = document.get("data_version")
    manifest_checksum = document.get("manifest_checksum")
    config_checksum = document.get("config_checksum")
    evidence = document.get("fixture_evidence")
    if not isinstance(model_version, str) or not MODEL_VERSION_RE.fullmatch(model_version):
        _fail("BLOCKED_INPUT", "invalid_model_version", "invalid model_version", BLOCKED_INPUT)
    if not isinstance(data_version, str) or not MODEL_VERSION_RE.fullmatch(data_version):
        _fail("BLOCKED_INPUT", "invalid_data_version", "invalid data_version", BLOCKED_INPUT)
    if not isinstance(manifest_checksum, str) or not SHA256_RE.fullmatch(manifest_checksum):
        _fail(
            "BLOCKED_INPUT",
            "invalid_manifest_checksum",
            "bundle manifest_checksum is missing or invalid",
            BLOCKED_INPUT,
        )
    if not isinstance(config_checksum, str) or not SHA256_RE.fullmatch(config_checksum):
        _fail(
            "BLOCKED_INPUT",
            "invalid_config_checksum",
            "bundle config_checksum is missing or invalid",
            BLOCKED_INPUT,
        )
    if not isinstance(evidence, dict):
        _fail(
            "BLOCKED_INPUT",
            "official_smoke_evidence_missing",
            "bundle fixture_evidence is required",
            BLOCKED_INPUT,
        )
    evidence_kind = evidence.get("kind")
    if evidence.get("dssm") is not True or evidence.get("deepfm") is not True:
        _fail(
            "BLOCKED_INPUT",
            "two_stage_evidence_missing",
            "fixture bundle must prove DSSM and DeepFM stages",
            BLOCKED_INPUT,
        )
    allowed_kind = "synthetic_protocol_test" if protocol_test_only else "official_smoke_two_stage"
    if evidence_kind != allowed_kind:
        _fail(
            "BLOCKED_INPUT",
            "official_smoke_evidence_missing",
            f"fixture evidence must be {allowed_kind}",
            BLOCKED_INPUT,
        )
    return BundleEvidence(
        path=path,
        model_version=model_version,
        data_version=data_version,
        artifact_checksum=actual,
        manifest_checksum=manifest_checksum,
        config_checksum=config_checksum,
        evidence_kind=str(evidence_kind),
        validated_payload=payload,
    )


def _docker_binary(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("docker")
    if found:
        return found
    desktop = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
    if desktop.is_file():
        return str(desktop)
    return "docker"


def _read_credentials_at(parent_descriptor: int) -> dict[str, str]:
    try:
        before = os.stat(".fixture-env", dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _fail(
            "DIRTY_FIXTURE",
            "fixture_credentials_missing",
            "fixture credential file is missing",
            DIRTY_FIXTURE,
        )
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail(
            "DIRTY_FIXTURE",
            "unsafe_fixture_credentials",
            "fixture credential file must be a regular non-symlink file",
            DIRTY_FIXTURE,
        )
    if stat.S_IMODE(before.st_mode) != 0o600:
        _fail(
            "DIRTY_FIXTURE",
            "unsafe_fixture_credential_permissions",
            "fixture credential file must have mode 0600",
            DIRTY_FIXTURE,
        )
    try:
        descriptor = os.open(
            ".fixture-env",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
    except OSError:
        _fail(
            "DIRTY_FIXTURE",
            "unsafe_fixture_credentials",
            "fixture credential file could not be opened safely",
            DIRTY_FIXTURE,
        )
    try:
        metadata = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail(
                "DIRTY_FIXTURE",
                "unsafe_fixture_credentials",
                "fixture credential file changed while opening",
                DIRTY_FIXTURE,
            )
        payload = os.read(descriptor, 8193)
        after = os.fstat(descriptor)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            _fail(
                "DIRTY_FIXTURE",
                "unsafe_fixture_credentials",
                "fixture credential file changed during reading",
                DIRTY_FIXTURE,
            )
    finally:
        os.close(descriptor)
    if len(payload) > 8192:
        _fail(
            "DIRTY_FIXTURE",
            "invalid_fixture_credentials",
            "fixture credential file is unexpectedly large",
            DIRTY_FIXTURE,
        )
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(
            "DIRTY_FIXTURE",
            "invalid_fixture_credentials",
            "fixture credential file is invalid",
            DIRTY_FIXTURE,
        )
    required = {
        "FIXTURE_POSTGRES_PASSWORD",
        "FIXTURE_JWT_SECRET",
        "FIXTURE_PUBLISH_TOKEN",
        "FIXTURE_SEED_PASSWORD",
    }
    if not isinstance(document, dict) or set(document) != required:
        _fail(
            "DIRTY_FIXTURE",
            "invalid_fixture_credentials",
            "fixture credential keys do not match the exact contract",
            DIRTY_FIXTURE,
        )
    result = {str(key): str(value) for key, value in document.items()}
    if any(len(value) < 32 for value in result.values()):
        _fail(
            "DIRTY_FIXTURE",
            "invalid_fixture_credentials",
            "fixture credentials do not meet the minimum length",
            DIRTY_FIXTURE,
        )
    return result


def _load_or_create_credentials(
    repo_root: Path, spec: FixtureSpec, *, existing_docker_resources: bool
) -> dict[str, str]:
    root_descriptor = _open_root_directory(
        repo_root,
        code="symlink_artifact_namespace_forbidden",
        exit_code=INVALID_INPUT,
    )
    directory_descriptors = [root_descriptor]
    try:
        for component in ("artifacts", "experiments", "7b", spec.fixture_id):
            child = _open_child_directory(
                directory_descriptors[-1],
                component,
                create=not existing_docker_resources,
                missing_status="DIRTY_FIXTURE",
                missing_code="fixture_credentials_missing",
                missing_message="existing Docker fixture has no credential namespace",
                missing_exit_code=DIRTY_FIXTURE,
                unsafe_code="symlink_artifact_namespace_forbidden",
                unsafe_message="fixture artifact namespace must contain only real directories",
                unsafe_exit_code=INVALID_INPUT,
            )
            directory_descriptors.append(child)
        parent_descriptor = directory_descriptors[-1]
        try:
            os.stat(".fixture-env", dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if existing_docker_resources:
                _fail(
                    "DIRTY_FIXTURE",
                    "fixture_credentials_missing",
                    "existing Docker fixture has no credential marker; choose a new ID",
                    DIRTY_FIXTURE,
                )
        else:
            return _read_credentials_at(parent_descriptor)
        credentials = {
            "FIXTURE_POSTGRES_PASSWORD": secrets.token_urlsafe(36),
            "FIXTURE_JWT_SECRET": secrets.token_urlsafe(48),
            "FIXTURE_PUBLISH_TOKEN": secrets.token_urlsafe(48),
            "FIXTURE_SEED_PASSWORD": secrets.token_urlsafe(36),
        }
        try:
            descriptor = os.open(
                ".fixture-env",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            # Do not accept a concurrently supplied secret file in a new fixture run.
            _fail(
                "DIRTY_FIXTURE",
                "fixture_credentials_raced",
                "fixture credential file appeared concurrently; choose a new ID",
                DIRTY_FIXTURE,
            )
        try:
            payload = _canonical(credentials) + b"\n"
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent_descriptor)
        return _read_credentials_at(parent_descriptor)
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)


def _fixed_event_graph(spec: FixtureSpec, model_version: str) -> dict[str, Any]:
    def stable(name: str) -> str:
        return str(uuid.uuid5(FIXED_GRAPH_NAMESPACE, f"{spec.fixture_id}:{name}"))

    events = []
    for index, event_type in enumerate(
        ("impression", "click", "like", "not_interested", "dwell", "revisit", "share")
    ):
        events.append(
            {
                "event_id": stable(f"event:{event_type}"),
                "event_type": event_type,
                "server_timestamp": f"2026-09-01T00:00:{10 + index:02d}+00:00",
                "duration_ms": 12000 if event_type == "dwell" else None,
            }
        )
    return {
        "item_id": f"fixture-{spec.fixture_id}-item",
        "snapshot_id": stable("snapshot"),
        "request_id": stable("request"),
        "exposure_id": stable("exposure"),
        "model_version": model_version,
        "events": events,
    }


def _credentials_checksum(credentials: dict[str, str]) -> str:
    return _sha256(_canonical(credentials))


def _require_free_loopback_ports(spec: FixtureSpec) -> None:
    for name, port in (
        ("db", spec.db_port),
        ("redis", spec.redis_port),
        ("api", spec.api_port),
    ):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            _fail(
                "BLOCKED_INPUT",
                "fixture_port_unavailable",
                f"derived isolated {name} port is unavailable; choose a new fixture ID",
                BLOCKED_INPUT,
            )
        finally:
            probe.close()


def _verify_container_credentials(
    services: dict[str, dict[str, Any]], environment: dict[str, str]
) -> None:
    def container_environment(service: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for entry in services[service].get("Config", {}).get("Env") or []:
            key, separator, value = str(entry).partition("=")
            if separator:
                values[key] = value
        return values

    db_environment = container_environment("db")
    api_environment = container_environment("api")
    expected_api = {
        "JWT_SECRET": environment["FIXTURE_JWT_SECRET"],
        "PUBLISH_TOKEN": environment["FIXTURE_PUBLISH_TOKEN"],
        "MICROLENS_SEED_PASSWORD": environment["FIXTURE_SEED_PASSWORD"],
        "DATABASE_URL": (
            "postgresql+psycopg://microlens_7b:"
            f"{environment['FIXTURE_POSTGRES_PASSWORD']}@db:5432/microlens_7b"
        ),
    }
    matches = db_environment.get("POSTGRES_PASSWORD") == environment[
        "FIXTURE_POSTGRES_PASSWORD"
    ] and all(api_environment.get(key) == value for key, value in expected_api.items())
    if not matches:
        _fail(
            "DIRTY_FIXTURE",
            "fixture_credentials_do_not_match_containers",
            "stored fixture credentials do not match the running isolated containers",
            DIRTY_FIXTURE,
        )


def _compose_base(
    docker: str, *, repo_root: Path, compose_file: Path, spec: FixtureSpec
) -> list[str]:
    return [
        docker,
        "compose",
        "--project-directory",
        str(repo_root),
        "-f",
        str(compose_file),
        "-p",
        spec.project,
    ]


def _resource_ids(runner: Runner, docker: str, kind: str, project: str) -> list[str]:
    if kind == "container":
        arguments = [docker, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
    else:
        arguments = [
            docker,
            kind,
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ]
    return [line for line in runner.run(arguments).decode().splitlines() if line]


def _inspect(runner: Runner, docker: str, identifiers: list[str]) -> list[dict[str, Any]]:
    if not identifiers:
        return []
    return json.loads(runner.run([docker, "inspect", *identifiers]))


def _project_resources(
    runner: Runner, docker: str, project: str
) -> dict[str, list[dict[str, Any]]]:
    return {
        kind: _inspect(runner, docker, _resource_ids(runner, docker, kind, project))
        for kind in ("container", "volume", "network")
    }


def _labels(item: dict[str, Any], kind: str) -> dict[str, str]:
    if kind == "container":
        return item.get("Config", {}).get("Labels") or {}
    return item.get("Labels") or {}


def _service_containers(resources: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in resources["container"]:
        service = (_labels(item, "container")).get("com.docker.compose.service")
        if service:
            if service in result:
                _fail(
                    "DIRTY_FIXTURE",
                    "duplicate_fixture_service",
                    "fixture has duplicate service containers",
                    DIRTY_FIXTURE,
                )
            result[service] = item
    return result


def _validate_existing_resources(
    resources: dict[str, list[dict[str, Any]]], spec: FixtureSpec
) -> dict[str, dict[str, Any]]:
    for kind, items in resources.items():
        for item in items:
            labels = _labels(item, kind)
            if labels.get("com.docker.compose.project") != spec.project:
                _fail(
                    "DIRTY_FIXTURE",
                    "project_label_mismatch",
                    "existing fixture resource project label mismatch",
                    DIRTY_FIXTURE,
                )
            if labels.get(LABEL_KIND) != FIXTURE_KIND or labels.get(LABEL_ID) != spec.fixture_id:
                _fail(
                    "DIRTY_FIXTURE",
                    "fixture_marker_label_mismatch",
                    "existing fixture resource marker/label mismatch",
                    DIRTY_FIXTURE,
                )
            name = str(item.get("Name", "")).lstrip("/")
            if not name.startswith(spec.project):
                _fail(
                    "DIRTY_FIXTURE",
                    "resource_allowlist_mismatch",
                    "existing resource name is outside the fixture allowlist",
                    DIRTY_FIXTURE,
                )
    services = _service_containers(resources)
    if set(services) != {"api", "db", "redis"}:
        _fail(
            "DIRTY_FIXTURE",
            "incomplete_fixture_resources",
            "existing fixture is partial or contains unexpected services; choose a new ID",
            DIRTY_FIXTURE,
        )
    if any(item.get("State", {}).get("Running") is not True for item in services.values()):
        _fail(
            "DIRTY_FIXTURE",
            "stopped_fixture_is_dirty",
            "existing fixture is not fully running; choose a new ID",
            DIRTY_FIXTURE,
        )
    expected_volumes = {
        f"{spec.project}_postgres_data",
        f"{spec.project}_redis_data",
        f"{spec.project}_model_artifacts",
        f"{spec.project}_training_exports",
    }
    if {str(item.get("Name")) for item in resources["volume"]} != expected_volumes:
        _fail(
            "DIRTY_FIXTURE",
            "fixture_volume_allowlist_mismatch",
            "fixture volume set does not match the exact allowlist",
            DIRTY_FIXTURE,
        )
    if {str(item.get("Name")) for item in resources["network"]} != {f"{spec.project}_backend"}:
        _fail(
            "DIRTY_FIXTURE",
            "fixture_network_allowlist_mismatch",
            "fixture network set does not match the exact allowlist",
            DIRTY_FIXTURE,
        )
    return services


def _exec(runner: Runner, docker: str, container: dict[str, Any], command: list[str]) -> bytes:
    return runner.run([docker, "exec", str(container["Id"]), *command])


def _snapshot_project(
    runner: Runner,
    docker: str,
    project: str,
    *,
    exclude_fixture_marker: bool,
) -> dict[str, str]:
    resources = _project_resources(runner, docker, project)
    services = _service_containers(resources)
    resource_shape = []
    for kind, items in resources.items():
        for item in items:
            labels = _labels(item, kind)
            resource_shape.append(
                {
                    "kind": kind,
                    "name": str(item.get("Name", "")).lstrip("/"),
                    "project": labels.get("com.docker.compose.project"),
                    "service": labels.get("com.docker.compose.service"),
                    "volume": labels.get("com.docker.compose.volume"),
                    "network": labels.get("com.docker.compose.network"),
                }
            )
    digests = {
        "resources": _sha256(
            _canonical(sorted(resource_shape, key=lambda row: (row["kind"], row["name"])))
        )
    }
    if {"db", "redis", "api"}.issubset(services) and all(
        services[name].get("State", {}).get("Running") is True for name in ("db", "redis", "api")
    ):
        dump_command = (
            'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
            "--data-only --inserts --no-owner --no-privileges"
        )
        if exclude_fixture_marker:
            dump_command += " --exclude-table=fixture_environment_markers"
        dump = ["sh", "-c", dump_command]
        dump_payload = _exec(runner, docker, services["db"], dump)
        # PostgreSQL 16 emits a fresh psql meta-command nonce on every dump.
        # It is transport protection, not database state, so remove only those
        # two lines before hashing the otherwise byte-exact canonical dump.
        normalized_dump = b"\n".join(
            line
            for line in dump_payload.splitlines()
            if not line.startswith((b"\\restrict ", b"\\unrestrict "))
        )
        digests["database"] = _sha256(normalized_dump)
        model_table_exists = (
            _exec(
                runner,
                docker,
                services["db"],
                [
                    "sh",
                    "-c",
                    'psql -X -A -t -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '
                    "\"SELECT to_regclass('model_versions') IS NOT NULL\"",
                ],
            )
            .decode()
            .strip()
        )
        if model_table_exists == "t":
            model_rows = _exec(
                runner,
                docker,
                services["db"],
                [
                    "sh",
                    "-c",
                    'psql -X -A -t -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '
                    "\"SELECT model_version || '|' || artifact_checksum || '|' || "
                    "manifest_checksum || '|' || status FROM model_versions "
                    'ORDER BY model_version"',
                ],
            )
        else:
            model_rows = b"model_versions:not_present\n"
        digests["model_registry"] = _sha256(model_rows)
        redis_dump = _exec(
            runner,
            docker,
            services["redis"],
            [
                "redis-cli",
                "--raw",
                "EVAL",
                "local ks=redis.call('KEYS','*'); table.sort(ks); local o={}; "
                "for _,k in ipairs(ks) do table.insert(o,k); "
                "table.insert(o,redis.call('DUMP',k) or '') end; return o",
                "0",
            ],
        )
        digests["redis"] = _sha256(redis_dump)
        artifact_dump = _exec(
            runner,
            docker,
            services["api"],
            [
                "python",
                "-c",
                "import hashlib,json,os,pathlib; "
                "roots=[pathlib.Path(os.environ['MODEL_ARTIFACTS_DIR']),"
                "pathlib.Path(os.environ.get('TRAINING_EXPORTS_DIR','/nonexistent'))]; "
                "rows=[]; [(rows.append((str(p.relative_to(r)),"
                "hashlib.sha256(p.read_bytes()).hexdigest()))) for r in roots "
                "if r.exists() for p in sorted(r.rglob('*')) "
                "if p.is_file() and not p.is_symlink()]; "
                "print(json.dumps(rows,separators=(',',':')))",
            ],
        )
        digests["artifacts"] = _sha256(artifact_dump)
    else:
        unavailable = _sha256(b"not-running-or-absent")
        digests.update(
            database=unavailable,
            model_registry=unavailable,
            redis=unavailable,
            artifacts=unavailable,
        )
    digests["total"] = _sha256(_canonical(digests))
    return digests


def _marker(
    runner: Runner, docker: str, services: dict[str, dict[str, Any]]
) -> dict[str, str] | None:
    exists = (
        _exec(
            runner,
            docker,
            services["db"],
            [
                "sh",
                "-c",
                'psql -X -A -t -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                "-c \"SELECT to_regclass('fixture_environment_markers') IS NOT NULL\"",
            ],
        )
        .decode()
        .strip()
    )
    if exists != "t":
        return None
    sql = (
        "SELECT row_to_json(t)::text FROM (SELECT fixture_id,compose_project,model_a_checksum,"
        "model_b_checksum,evidence_kind,event_graph_checksum,credentials_checksum,"
        "database_checksum,"
        "redis_checksum,artifact_checksum "
        "FROM fixture_environment_markers WHERE environment_kind='7b_fixture') t"
    )
    output = (
        _exec(
            runner,
            docker,
            services["db"],
            ["sh", "-c", f'psql -X -A -t -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{sql}"'],
        )
        .decode()
        .strip()
    )
    if not output:
        return None
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        _fail(
            "DIRTY_FIXTURE",
            "invalid_database_marker",
            "fixture database marker is invalid",
            DIRTY_FIXTURE,
        )
    return {str(key): str(item) for key, item in value.items()}


def _verify_existing_clean(
    runner: Runner,
    docker: str,
    spec: FixtureSpec,
    services: dict[str, dict[str, Any]],
    bundles: tuple[BundleEvidence, BundleEvidence],
    evidence_kind: str,
    credentials_checksum: str,
) -> None:
    marker = _marker(runner, docker, services)
    if marker is None:
        _fail(
            "DIRTY_FIXTURE",
            "database_marker_missing",
            "existing fixture has no verified database marker; choose a new ID",
            DIRTY_FIXTURE,
        )
    expected = {
        "fixture_id": spec.fixture_id,
        "compose_project": spec.project,
        "model_a_checksum": bundles[0].artifact_checksum,
        "model_b_checksum": bundles[1].artifact_checksum,
        "evidence_kind": evidence_kind,
        "event_graph_checksum": _sha256(
            _canonical(_fixed_event_graph(spec, bundles[0].model_version))
        ),
        "credentials_checksum": credentials_checksum,
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        _fail(
            "DIRTY_FIXTURE",
            "database_marker_mismatch",
            "existing fixture marker does not match requested inputs; choose a new ID",
            DIRTY_FIXTURE,
        )
    snapshot = _snapshot_project(runner, docker, spec.project, exclude_fixture_marker=True)
    for marker_key, snapshot_key in (
        ("database_checksum", "database"),
        ("redis_checksum", "redis"),
        ("artifact_checksum", "artifacts"),
    ):
        if marker.get(marker_key) != snapshot[snapshot_key]:
            _fail(
                "DIRTY_FIXTURE",
                "fixture_state_is_dirty",
                "existing fixture changed after preparation; choose a new ID",
                DIRTY_FIXTURE,
            )


def _compose_exec(
    runner: Runner,
    compose: list[str],
    environment: dict[str, str],
    service: str,
    command: list[str],
    *,
    input_payload: bytes | None = None,
) -> bytes:
    arguments = [*compose, "exec", "-T", service, *command]
    if input_payload is None:
        return runner.run(arguments, environment=environment)
    return runner.run(arguments, environment=environment, input_payload=input_payload)


def _registration_policy(evidence_kind: str) -> tuple[str, str, str, str]:
    if evidence_kind == "synthetic_protocol_test":
        return "systems_only", "non_comparable", "false", "EVALUATED"
    if evidence_kind == "official_smoke_two_stage":
        return "base_official", "comparable", "true", "READY"
    _fail(
        "BLOCKED_INPUT",
        "unknown_model_evidence",
        "model evidence kind has no safe registry policy",
        BLOCKED_INPUT,
    )


def _stage_and_register(
    runner: Runner,
    docker: str,
    compose: list[str],
    environment: dict[str, str],
    spec: FixtureSpec,
    bundles: tuple[BundleEvidence, BundleEvidence],
    evidence_kind: str,
) -> None:
    _compose_exec(
        runner,
        compose,
        environment,
        "api",
        ["python", "-m", "scripts.platform_commands", "migrate-seed"],
    )
    for bundle in bundles:
        if _sha256(bundle.validated_payload) != bundle.artifact_checksum:
            _fail(
                "SAFETY_CHECK_FAILED",
                "validated_bundle_memory_corrupted",
                "validated ModelBundle bytes no longer match their checksum",
                SAFETY_CHECK_FAILED,
            )
        relative = f"models/{bundle.model_version}/bundle.json"
        destination = f"{spec.artifact_namespace}/{relative}"
        _compose_exec(
            runner,
            compose,
            environment,
            "api",
            ["mkdir", "-p", f"{spec.artifact_namespace}/models/{bundle.model_version}"],
        )
        _compose_exec(
            runner,
            compose,
            environment,
            "api",
            [
                "python",
                "-c",
                "import os,sys; p=sys.argv[1]; "
                "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
                "f=os.fdopen(fd,'wb'); b=sys.stdin.buffer.read(); "
                "f.write(b); f.flush(); os.fsync(f.fileno()); f.close()",
                destination,
            ],
            input_payload=bundle.validated_payload,
        )
        copied = (
            _compose_exec(
                runner,
                compose,
                environment,
                "api",
                [
                    "python",
                    "-c",
                    "import hashlib; "
                    f"print(hashlib.sha256(open({destination!r},'rb').read()).hexdigest())",
                ],
            )
            .decode()
            .strip()
        )
        if copied != bundle.artifact_checksum:
            _fail(
                "SAFETY_CHECK_FAILED",
                "staged_bundle_checksum_mismatch",
                "staged bundle checksum mismatch",
                SAFETY_CHECK_FAILED,
            )
        purpose, comparability, eligible, status = _registration_policy(evidence_kind)
        sql = (
            "INSERT INTO model_versions(model_version,data_version,config_checksum,metrics,"
            "artifact_uri,artifact_checksum,manifest_checksum,purpose,evaluation_comparability,"
            "activation_eligible,status,trained_at) VALUES ("
            f"'{bundle.model_version}','{bundle.data_version}','{bundle.config_checksum}',"
            f"'{{}}'::jsonb,'{relative}','{bundle.artifact_checksum}',"
            f"'{bundle.manifest_checksum}','{purpose}','{comparability}',{eligible},"
            f"'{status}',now()) "
            "ON CONFLICT (model_version) DO NOTHING; "
            "SELECT model_version || '|' || artifact_checksum || '|' || "
            "manifest_checksum || '|' || status "
            f"FROM model_versions WHERE model_version='{bundle.model_version}';"
        )
        result = (
            _compose_exec(
                runner,
                compose,
                environment,
                "db",
                [
                    "psql",
                    "-X",
                    "-A",
                    "-t",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    "microlens_7b",
                    "-d",
                    "microlens_7b",
                    "-c",
                    sql,
                ],
            )
            .decode()
            .strip()
            .splitlines()
        )
        expected = (
            f"{bundle.model_version}|{bundle.artifact_checksum}|{bundle.manifest_checksum}|{status}"
        )
        if not result or result[-1] != expected:
            _fail(
                "DIRTY_FIXTURE",
                "model_registry_conflict",
                "existing model registry row conflicts with the verified bundle",
                DIRTY_FIXTURE,
            )


def _seed_fixed_event_graph(
    runner: Runner,
    compose: list[str],
    environment: dict[str, str],
    spec: FixtureSpec,
    model_version: str,
) -> str:
    graph = _fixed_event_graph(spec, model_version)
    payload_hash = hashlib.sha256(b"{}").hexdigest()
    event_sql = []
    for event in graph["events"]:
        duration = "12000" if event["duration_ms"] is not None else "NULL"
        event_sql.append(
            "INSERT INTO events(event_id,exposure_id,request_id,user_id,item_id,position,"
            "feed_type,source,event_type,client_timestamp,server_timestamp,duration_ms,"
            "payload,payload_hash) SELECT "
            f"'{event['event_id']}'::uuid,e.id,r.request_id,r.user_id,e.item_id,e.position,"
            f"'personalized','fixture_7b','{event['event_type']}',NULL,"
            f"'{event['server_timestamp']}'::timestamptz,{duration},'{{}}'::jsonb,"
            f"'{payload_hash}' FROM exposures e JOIN recommendation_requests r "
            f"ON r.request_id=e.request_id WHERE e.id='{graph['exposure_id']}'::uuid "
            "ON CONFLICT (event_id) DO NOTHING;"
        )
    sql = (
        "INSERT INTO items(id,title,metadata_status,online_status,state_version,updated_at) "
        f"VALUES ('{graph['item_id']}','Deterministic Phase 7B fixture item','complete',"
        "'online',0,'2026-09-01T00:00:00+00:00') ON CONFLICT (id) DO NOTHING;"
        "INSERT INTO recommendation_snapshots(snapshot_id,user_id,feed_type,model_version,"
        "snapshot_seed,expires_at,created_at) SELECT "
        f"'{graph['snapshot_id']}'::uuid,u.id,'personalized','{model_version}',7001,"
        "'2036-09-01T00:00:00+00:00','2026-09-01T00:00:01+00:00' FROM users u "
        "WHERE u.username_normalized='demo_user_a' ON CONFLICT (snapshot_id) DO NOTHING;"
        'INSERT INTO recommendation_requests(request_id,snapshot_id,user_id,"offset","limit",'
        "latency_ms,created_at) SELECT "
        f"'{graph['request_id']}'::uuid,s.snapshot_id,s.user_id,0,1,1,"
        "'2026-09-01T00:00:02+00:00' FROM recommendation_snapshots s "
        f"WHERE s.snapshot_id='{graph['snapshot_id']}'::uuid "
        "ON CONFLICT (request_id) DO NOTHING;"
        "INSERT INTO recommendation_snapshot_items(snapshot_id,item_id,source,raw_score,"
        "normalized_score,filter_reason,snapshot_position,promotion_rule_id) VALUES ("
        f"'{graph['snapshot_id']}'::uuid,'{graph['item_id']}','fixture_7b',1.0,1.0,NULL,0,NULL) "
        "ON CONFLICT (snapshot_id,item_id) DO NOTHING;"
        "INSERT INTO exposures(id,request_id,snapshot_id,user_id,item_id,position,source,"
        "model_version,exposed_at) SELECT "
        f"'{graph['exposure_id']}'::uuid,r.request_id,r.snapshot_id,r.user_id,"
        f"'{graph['item_id']}',0,'fixture_7b','{model_version}',"
        "'2026-09-01T00:00:03+00:00' FROM recommendation_requests r "
        f"WHERE r.request_id='{graph['request_id']}'::uuid ON CONFLICT (id) DO NOTHING;"
        + "".join(event_sql)
        + "SELECT count(*)::text || '|' || string_agg(event_type::text,',' ORDER BY "
        "server_timestamp) FROM events WHERE request_id="
        f"'{graph['request_id']}'::uuid;"
    )
    result = (
        _compose_exec(
            runner,
            compose,
            environment,
            "db",
            [
                "psql",
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "microlens_7b",
                "-d",
                "microlens_7b",
                "-c",
                sql,
            ],
        )
        .decode()
        .strip()
        .splitlines()
    )
    expected = "7|impression,click,like,not_interested,dwell,revisit,share"
    if not result or result[-1] != expected:
        _fail(
            "DIRTY_FIXTURE",
            "fixed_event_graph_conflict",
            "deterministic request/exposure/event graph failed exact FK-backed validation",
            DIRTY_FIXTURE,
        )
    return _sha256(_canonical(graph))


def _write_marker(
    runner: Runner,
    compose: list[str],
    environment: dict[str, str],
    spec: FixtureSpec,
    bundles: tuple[BundleEvidence, BundleEvidence],
    evidence_kind: str,
    event_graph_checksum: str,
    credentials_checksum: str,
    snapshot: dict[str, str],
) -> None:
    sql = (
        "CREATE TABLE IF NOT EXISTS fixture_environment_markers ("
        "environment_kind text PRIMARY KEY,fixture_id text NOT NULL,compose_project text NOT NULL,"
        "model_a_checksum text NOT NULL,model_b_checksum text NOT NULL,evidence_kind text NOT NULL,"
        "event_graph_checksum text NOT NULL,"
        "credentials_checksum text NOT NULL,"
        "database_checksum text NOT NULL,redis_checksum text NOT NULL,"
        "artifact_checksum text NOT NULL,"
        "created_at timestamptz NOT NULL DEFAULT now()); "
        "INSERT INTO fixture_environment_markers(environment_kind,fixture_id,compose_project,"
        "model_a_checksum,model_b_checksum,evidence_kind,event_graph_checksum,"
        "credentials_checksum,database_checksum,redis_checksum,artifact_checksum) "
        f"VALUES ('7b_fixture','{spec.fixture_id}','{spec.project}',"
        f"'{bundles[0].artifact_checksum}','{bundles[1].artifact_checksum}',"
        f"'{evidence_kind}','{event_graph_checksum}','{credentials_checksum}',"
        f"'{snapshot['database']}','{snapshot['redis']}','{snapshot['artifacts']}') "
        "ON CONFLICT DO NOTHING;"
    )
    _compose_exec(
        runner,
        compose,
        environment,
        "db",
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "microlens_7b",
            "-d",
            "microlens_7b",
            "-c",
            sql,
        ],
    )


def prepare(arguments: argparse.Namespace, *, runner: Runner | None = None) -> int:
    runner = runner or Runner()
    repo_root = Path(__file__).resolve().parents[1]
    spec = FixtureSpec.create(arguments.fixture_id)
    bundle_a = validate_bundle(
        arguments.model_bundle_a,
        arguments.model_bundle_a_sha256,
        repo_root=repo_root,
        protocol_test_only=arguments.protocol_test_only,
    )
    bundle_b = validate_bundle(
        arguments.model_bundle_b,
        arguments.model_bundle_b_sha256,
        repo_root=repo_root,
        protocol_test_only=arguments.protocol_test_only,
    )
    bundles = (bundle_a, bundle_b)
    if bundle_a.model_version == bundle_b.model_version:
        _fail(
            "BLOCKED_INPUT",
            "duplicate_model_version",
            "the two fixture ModelBundles must have distinct model versions",
            BLOCKED_INPUT,
        )
    if bundle_a.artifact_checksum == bundle_b.artifact_checksum:
        _fail(
            "BLOCKED_INPUT",
            "duplicate_model_bundle",
            "the two fixture ModelBundles must have distinct checksums",
            BLOCKED_INPUT,
        )
    evidence_kind = (
        "synthetic_protocol_test" if arguments.protocol_test_only else "official_smoke_two_stage"
    )
    compose_file = repo_root / "scripts" / "compose.7b.yaml"
    if compose_file.is_symlink() or not compose_file.is_file():
        _fail(
            "SAFETY_CHECK_FAILED",
            "unsafe_compose_file",
            "7B Compose file must be a regular non-symlink file",
            SAFETY_CHECK_FAILED,
        )
    docker = _docker_binary(arguments.docker)
    allowlist = {
        "project": spec.project,
        "services": ["api", "db", "redis"],
        "volumes": [
            f"{spec.project}_postgres_data",
            f"{spec.project}_redis_data",
            f"{spec.project}_model_artifacts",
            f"{spec.project}_training_exports",
        ],
        "network": f"{spec.project}_backend",
        "host_ports": {
            "db": spec.db_port,
            "redis": spec.redis_port,
            "api": spec.api_port,
        },
        "artifact_namespace": spec.artifact_namespace,
        "forbidden": [
            "connect_default_project_for_write",
            "flushall",
            "truncate",
            "delete_volume",
            "automatic_cleanup",
        ],
        "model_evidence": evidence_kind,
    }
    print(f"status=PREVIEW allowlist={json.dumps(allowlist, sort_keys=True)}")
    runner.run([docker, "version", "--format", "{{.Server.Version}}"])
    resources = _project_resources(runner, docker, spec.project)
    existing = any(resources.values())
    if not existing:
        _require_free_loopback_ports(spec)
    credentials = _load_or_create_credentials(repo_root, spec, existing_docker_resources=existing)
    environment = spec.environment(credentials)
    compose = _compose_base(docker, repo_root=repo_root, compose_file=compose_file, spec=spec)
    runner.run([*compose, "config", "--quiet"], environment=environment)
    if existing:
        services = _validate_existing_resources(resources, spec)
        _verify_container_credentials(services, environment)
        _verify_existing_clean(
            runner,
            docker,
            spec,
            services,
            bundles,
            evidence_kind,
            _credentials_checksum(credentials),
        )
        default_before = _snapshot_project(
            runner, docker, DEFAULT_PROJECT, exclude_fixture_marker=False
        )
        default_after = _snapshot_project(
            runner, docker, DEFAULT_PROJECT, exclude_fixture_marker=False
        )
        if default_before != default_after:
            _fail(
                "SAFETY_CHECK_FAILED",
                "default_stack_changed",
                "default stack changed during idempotency verification",
                SAFETY_CHECK_FAILED,
            )
        print(
            "status=PASS mode=idempotent "
            f"project={spec.project} default_stack_checksum={default_after['total']} "
            f"model_evidence={evidence_kind}"
        )
        return 0

    default_before = _snapshot_project(
        runner, docker, DEFAULT_PROJECT, exclude_fixture_marker=False
    )
    runner.run(
        [*compose, "up", "-d", "--build", "--wait", "db", "redis", "api"],
        environment=environment,
    )
    created = _project_resources(runner, docker, spec.project)
    services = _validate_existing_resources(created, spec)
    _verify_container_credentials(services, environment)
    _stage_and_register(runner, docker, compose, environment, spec, bundles, evidence_kind)
    event_graph_checksum = _seed_fixed_event_graph(
        runner, compose, environment, spec, bundles[0].model_version
    )
    clean_snapshot = _snapshot_project(runner, docker, spec.project, exclude_fixture_marker=True)
    _write_marker(
        runner,
        compose,
        environment,
        spec,
        bundles,
        evidence_kind,
        event_graph_checksum,
        _credentials_checksum(credentials),
        clean_snapshot,
    )
    marker = _marker(runner, docker, services)
    if marker is None:
        _fail(
            "SAFETY_CHECK_FAILED",
            "database_marker_write_failed",
            "fixture database marker could not be verified",
            SAFETY_CHECK_FAILED,
        )
    _verify_existing_clean(
        runner,
        docker,
        spec,
        services,
        bundles,
        evidence_kind,
        _credentials_checksum(credentials),
    )
    default_after = _snapshot_project(runner, docker, DEFAULT_PROJECT, exclude_fixture_marker=False)
    if default_before != default_after:
        _fail(
            "SAFETY_CHECK_FAILED",
            "default_stack_changed",
            "default DB/Redis/model registry/artifacts changed during fixture preparation",
            SAFETY_CHECK_FAILED,
        )
    print(
        "status=PASS mode=created "
        f"project={spec.project} fixture_state_checksum={clean_snapshot['total']} "
        f"default_stack_before_checksum={default_before['total']} "
        f"default_stack_after_checksum={default_after['total']} "
        f"model_evidence={evidence_kind}"
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a fail-closed, non-destructive isolated Phase 7B fixture"
    )
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--model-bundle-a", default="")
    parser.add_argument("--model-bundle-a-sha256", default="")
    parser.add_argument("--model-bundle-b", default="")
    parser.add_argument("--model-bundle-b-sha256", default="")
    parser.add_argument("--docker")
    parser.add_argument("--protocol-test-only", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return prepare(parse_args(argv))
    except FixtureError as exc:
        print(
            f"status={exc.status} code={exc.code} message={exc}",
            file=sys.stderr,
        )
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
