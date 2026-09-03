from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# Formal Make targets disable site initialization and isolate Python paths/environment
# so ignored virtualenv hooks and repository module shadows cannot precede this gate.
if __name__ == "__main__" and (not sys.flags.isolated or not sys.flags.no_site):
    print("phase7a launcher refused: host Python must use both -I and -S", file=sys.stderr)
    raise SystemExit(2)
if sys.flags.isolated and sys.flags.no_site:
    _bootstrap_root = Path(__file__).resolve().parents[1]
    _bootstrap_environment = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    _bootstrap_status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        cwd=_bootstrap_root,
        check=False,
        capture_output=True,
        env=_bootstrap_environment,
    )
    if _bootstrap_status.returncode != 0 or _bootstrap_status.stdout:
        print(
            "phase7a launcher refused: formal Phase 7A requires an entirely clean Git worktree",
            file=sys.stderr,
        )
        raise SystemExit(2)
    sys.path.insert(0, str(_bootstrap_root))

from recsys.data.common import SHA256_PATTERN, validate_relative_file_name
from recsys.experiments.source_identity import ATTESTATION_PATH, validate_reviewed_source
from recsys.models.errors import ModelInputError

_IMAGE_PATTERN = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")
_GIT_SHA_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_CONTAINER_PROCESSED_ROOT = "/artifacts/processed"
_CONTAINER_RUN_ROOT = "/phase7a"
_FIXED_DOCKER_ARGUMENTS = (
    "--rm",
    "--pull=never",
    "--network=none",
    "--read-only",
    "--memory=5g",
    "--memory-swap=5g",
    "--cpus=4",
    "--pids-limit=512",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,nodev,size=256m",
)
_REQUIRED_ENVIRONMENT = (
    "DATA_VERSION",
    "DATA_MANIFEST_CHECKSUM",
    "GIT_REVISION",
    "RUN_ID",
    "PHASE7A_IMAGE",
    "PHASE7A_SOURCE_CHECKSUM",
    "PHASE7A_PROCESSED_ROOT",
    "PHASE7A_RUN_ROOT",
)
_BUILD_ENVIRONMENT = ("GIT_REVISION", "PHASE7A_SOURCE_CHECKSUM", "PHASE7A_BUILD_TAG")
_FIXED_BUILD_RESOURCES = (
    "memory=5g",
    "cpu-period=100000",
    "cpu-quota=400000",
)


def _required_environment(environment: Mapping[str, str]) -> dict[str, str]:
    missing = [name for name in _REQUIRED_ENVIRONMENT if not environment.get(name)]
    if missing:
        raise ModelInputError(f"required Phase 7A environment is missing: {', '.join(missing)}")
    return {name: environment[name] for name in _REQUIRED_ENVIRONMENT}


def _reject_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise ModelInputError(f"{label} contains a symlink component")


def _canonical_path(raw: str, *, label: str) -> Path:
    if "\x00" in raw or "\n" in raw or "," in raw:
        raise ModelInputError(f"{label} contains characters unsafe for a Docker bind mount")
    lexical = Path(raw)
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    _reject_symlink_components(lexical, label=label)
    return lexical.resolve(strict=False)


def _validate_inputs(
    *, mode: str, environment: Mapping[str, str], repo_root: Path
) -> tuple[dict[str, str], Path, Path]:
    values = _required_environment(environment)
    if not _IMAGE_PATTERN.fullmatch(values["PHASE7A_IMAGE"]):
        raise ModelInputError("PHASE7A_IMAGE must be an exact name@sha256:<64> reference")
    if not _GIT_SHA_PATTERN.fullmatch(values["GIT_REVISION"]):
        raise ModelInputError("GIT_REVISION must be an explicit lowercase 40-character SHA")
    for name in ("DATA_MANIFEST_CHECKSUM", "PHASE7A_SOURCE_CHECKSUM"):
        if not SHA256_PATTERN.fullmatch(values[name]):
            raise ModelInputError(f"{name} must be lowercase SHA-256")
    if values["DATA_VERSION"].lower() == "latest":
        raise ModelInputError("DATA_VERSION must not be latest")
    validate_relative_file_name(values["DATA_VERSION"])
    validate_relative_file_name(values["RUN_ID"])

    processed_root = _canonical_path(
        values["PHASE7A_PROCESSED_ROOT"], label="PHASE7A_PROCESSED_ROOT"
    )
    run_root = _canonical_path(values["PHASE7A_RUN_ROOT"], label="PHASE7A_RUN_ROOT")
    if processed_root.is_symlink() or not processed_root.is_dir():
        raise ModelInputError("PHASE7A_PROCESSED_ROOT must be an existing real directory")
    if (
        processed_root == run_root
        or processed_root in run_root.parents
        or run_root in processed_root.parents
    ):
        raise ModelInputError("processed and run roots must be distinct, non-overlapping paths")

    historical_root = (repo_root / "output" / "phase7a").resolve(strict=False)
    if run_root == historical_root:
        raise ModelInputError("existing run root is the historical Phase 7A output root")
    if historical_root in run_root.parents and run_root.parent != historical_root:
        raise ModelInputError(
            "run root may only be one fresh child below the historical output root"
        )
    if mode == "run":
        if run_root.exists() or run_root.is_symlink():
            raise ModelInputError("existing run root is refused; select a fresh namespace")
        if not run_root.parent.is_dir():
            raise ModelInputError("PHASE7A_RUN_ROOT parent must already exist")
    else:
        if run_root.is_symlink() or not run_root.is_dir():
            raise ModelInputError("preflight run root must be an existing real probe directory")
        if any(run_root.iterdir()):
            raise ModelInputError("existing run root for preflight must be empty")
        if not any("probe" in part.lower() for part in run_root.parts):
            raise ModelInputError("preflight run root must be an explicitly named probe path")

    actual_source_checksum = validate_reviewed_source(repo_root, values["GIT_REVISION"])
    if actual_source_checksum != values["PHASE7A_SOURCE_CHECKSUM"]:
        raise ModelInputError("PHASE7A_SOURCE_CHECKSUM does not match the requested clean commit")
    return values, processed_root, run_root


def build_image_argv(*, environment: Mapping[str, str], repo_root: str | Path = ".") -> list[str]:
    missing = [name for name in _BUILD_ENVIRONMENT if not environment.get(name)]
    if missing:
        raise ModelInputError(
            f"required Phase 7A build environment is missing: {', '.join(missing)}"
        )
    revision = environment["GIT_REVISION"]
    expected_checksum = environment["PHASE7A_SOURCE_CHECKSUM"]
    tag = environment["PHASE7A_BUILD_TAG"]
    if not _GIT_SHA_PATTERN.fullmatch(revision):
        raise ModelInputError("GIT_REVISION must be an explicit lowercase 40-character SHA")
    if not SHA256_PATTERN.fullmatch(expected_checksum):
        raise ModelInputError("PHASE7A_SOURCE_CHECKSUM must be lowercase SHA-256")
    if not tag or any(character.isspace() for character in tag) or "@" in tag:
        raise ModelInputError("PHASE7A_BUILD_TAG must be one immutable local build tag")
    root = Path(repo_root).resolve()
    actual_checksum = validate_reviewed_source(root, revision)
    if actual_checksum != expected_checksum:
        raise ModelInputError("PHASE7A_SOURCE_CHECKSUM does not match the requested clean commit")
    return [
        "docker",
        "buildx",
        "build",
        "--pull=false",
        "--load",
        "--resource",
        _FIXED_BUILD_RESOURCES[0],
        "--resource",
        _FIXED_BUILD_RESOURCES[1],
        "--resource",
        _FIXED_BUILD_RESOURCES[2],
        "--build-arg",
        f"GIT_REVISION={revision}",
        "--build-arg",
        f"PHASE7A_SOURCE_CHECKSUM={expected_checksum}",
        "-f",
        "apps/worker/Dockerfile",
        "-t",
        tag,
        str(root),
    ]


def build_docker_argv(
    *, mode: str, environment: Mapping[str, str], repo_root: str | Path = "."
) -> tuple[list[str], Path]:
    if mode not in {"run", "preflight"}:
        raise ValueError("mode must be run or preflight")
    root = Path(repo_root).resolve()
    values, processed_root, run_root = _validate_inputs(
        mode=mode, environment=environment, repo_root=root
    )
    inner = [
        "python",
        "-m",
        "recsys.experiments.phase7a_cli",
        mode,
        "--matrix",
        "configs/models/experiment-matrix.json",
        "--repo-root",
        "/workspace",
        "--processed-root",
        _CONTAINER_PROCESSED_ROOT,
        "--data-version",
        values["DATA_VERSION"],
        "--data-manifest-checksum",
        values["DATA_MANIFEST_CHECKSUM"],
        "--output-root",
        _CONTAINER_RUN_ROOT,
        "--run-id",
        values["RUN_ID"],
        "--git-revision",
        values["GIT_REVISION"],
        "--image-digest",
        values["PHASE7A_IMAGE"],
        "--source-checksum",
        values["PHASE7A_SOURCE_CHECKSUM"],
        "--attestation-path",
        str(ATTESTATION_PATH),
    ]
    argv = [
        "docker",
        "run",
        *_FIXED_DOCKER_ARGUMENTS,
        "--mount",
        f"type=bind,src={processed_root},dst={_CONTAINER_PROCESSED_ROOT},readonly",
        "--mount",
        f"type=bind,src={run_root},dst={_CONTAINER_RUN_ROOT}",
        values["PHASE7A_IMAGE"],
        *inner,
    ]
    return argv, run_root


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Launch isolated Phase 7A by exact OCI digest")
    value.add_argument("mode", choices=("checksum", "build", "run", "preflight"))
    value.add_argument("--repo-root", default=".")
    value.add_argument("--render", action="store_true")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    repo_root = Path(arguments.repo_root).resolve()
    if sys.flags.isolated and sys.flags.no_site and repo_root != _bootstrap_root:
        raise ModelInputError("repo root must be the isolated launcher's repository")
    if arguments.mode == "checksum":
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
        ).stdout.strip()
        print(validate_reviewed_source(repo_root, revision))
        return 0
    if arguments.mode == "build":
        docker_argv = build_image_argv(environment=os.environ, repo_root=arguments.repo_root)
        run_root = None
    else:
        docker_argv, run_root = build_docker_argv(
            mode=arguments.mode, environment=os.environ, repo_root=arguments.repo_root
        )
    if arguments.render:
        print(json.dumps(docker_argv))
        return 0
    if arguments.mode == "run" and run_root is not None:
        run_root.mkdir(mode=0o700)
    completed = subprocess.run(docker_argv, check=False)
    return completed.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ModelInputError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"phase7a launcher refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
