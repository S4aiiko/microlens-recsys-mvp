"""Read-only preflight helpers and fail-closed Phase 1 command placeholders."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STABLE_TARGETS = (
    "doctor data-inspect data-download smoke-all up up-core ps logs test down "
    "full-data train-full export-events build-training-data train-async "
    "worker-run-once job-status cache-stats publish prepare-7b-fixture covers"
)
REQUIRED_DATA_FILES = (
    "MicroLens-50k_pairs.csv",
    "MicroLens-50k_titles.csv",
    "MicroLens-50k_likes_and_views.txt",
)
DOCKER_CHECK_TIMEOUT_SECONDS = 5.0


def print_help() -> int:
    print("Stable Phase 1 command interface:")
    for target in STABLE_TARGETS.split():
        print(f"  make {target}")
    print("Unimplemented data/model/fixture targets fail closed with exit code 2.")
    return 0


def command_state(name: str) -> str:
    resolved = shutil.which(name)
    return resolved if resolved else "MISSING"


def port_state(port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        return "IN_USE" if client.connect_ex(("127.0.0.1", port)) == 0 else "AVAILABLE"


def _read_only_command_state(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except OSError:
        return "UNAVAILABLE"
    return "AVAILABLE" if result.returncode == 0 else "UNAVAILABLE"


def docker_preflight() -> tuple[dict[str, str], str]:
    docker_cli = shutil.which("docker")
    if docker_cli is None:
        return (
            {
                "docker_cli": "MISSING",
                "docker_compose": "NOT_CHECKED",
                "docker_daemon": "NOT_CHECKED",
            },
            "NOT_READY_DOCKER_CLI_MISSING",
        )

    compose_state = _read_only_command_state([docker_cli, "compose", "version"])
    if compose_state != "AVAILABLE":
        suffix = "TIMEOUT" if compose_state == "TIMEOUT" else "MISSING"
        return (
            {
                "docker_cli": docker_cli,
                "docker_compose": compose_state,
                "docker_daemon": "NOT_CHECKED",
            },
            f"NOT_READY_DOCKER_COMPOSE_{suffix}",
        )

    daemon_state = _read_only_command_state([docker_cli, "info", "--format", "{{.ServerVersion}}"])
    if daemon_state != "AVAILABLE":
        suffix = "TIMEOUT" if daemon_state == "TIMEOUT" else "UNAVAILABLE"
        return (
            {
                "docker_cli": docker_cli,
                "docker_compose": compose_state,
                "docker_daemon": daemon_state,
            },
            f"NOT_READY_DOCKER_DAEMON_{suffix}",
        )

    return (
        {
            "docker_cli": docker_cli,
            "docker_compose": compose_state,
            "docker_daemon": daemon_state,
        },
        "FOUNDATION_PREFLIGHT_READY",
    )


def doctor() -> int:
    disk = shutil.disk_usage(ROOT)
    docker_checks, docker_status = docker_preflight()
    checks = {
        "workspace": str(ROOT),
        **docker_checks,
        "git": command_state("git"),
        "node": command_state("node"),
        "npm": command_state("npm"),
        "python3": command_state("python3"),
        "web_port_5173": port_state(5173),
        "api_port_8000": port_state(8000),
        "disk_free_bytes": str(disk.free),
        "env_file_present": str((ROOT / ".env").exists()).lower(),
    }
    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"status={docker_status}")
    return 0 if docker_status == "FOUNDATION_PREFLIGHT_READY" else 2


def data_inspect() -> int:
    configured = Path(os.environ.get("MICROLENS_DATA_DIR", ROOT / "dataset")).expanduser()
    if not configured.is_absolute():
        configured = (ROOT / configured).resolve()
    print("mode=READ_ONLY_DISCOVERY")
    print(f"data_root={configured}")
    if not configured.exists():
        print("status=DATA_ROOT_MISSING")
        return 2

    missing: list[str] = []
    for filename in REQUIRED_DATA_FILES:
        matches = sorted(configured.rglob(filename))
        if not matches:
            missing.append(filename)
            print(f"required_file={filename} status=MISSING")
            continue
        for match in matches:
            print(
                f"required_file={filename} status=FOUND "
                f"size_bytes={match.stat().st_size} path={match}"
            )
    print("note=No file was moved, parsed, modified, or downloaded.")
    if missing:
        print(f"status=INCOMPLETE missing={','.join(missing)}")
        return 2
    print("status=FOUND_REQUIRED_FILENAMES_NOT_SCHEMA_VERIFIED")
    return 0


def placeholder(target: str) -> int:
    print(
        f"target={target} status=NOT_IMPLEMENTED_SAFE_PLACEHOLDER "
        "reason=Phase_1_freezes_the_interface_only",
        file=sys.stderr,
    )
    return 2


def require_docker() -> int:
    if shutil.which("docker") is None:
        print("status=NOT_RUN_DOCKER_MISSING", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "help":
        return print_help()
    if argv[0] == "doctor":
        return doctor()
    if argv[0] == "data-inspect":
        return data_inspect()
    if argv[0] == "placeholder" and len(argv) == 2:
        return placeholder(argv[1])
    if argv[0] == "require-docker":
        return require_docker()
    print(f"Unknown foundation command: {' '.join(argv)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
