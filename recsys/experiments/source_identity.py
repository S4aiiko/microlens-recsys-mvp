from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from recsys.data.common import canonical_json_bytes, sha256_bytes, sha256_file
from recsys.models.errors import ModelInputError

SOURCE_ROOTS = ("apps", "recsys", "configs")
SOURCE_FILES = (
    "Makefile",
    "requirements-model.lock",
    "requirements-data.lock",
    "requirements-api.lock",
    "requirements-search.lock",
    "requirements-analytics.lock",
    "scripts/phase7a_launcher.py",
)
CONTROL_FILES = (
    ".dockerignore",
    ".github/workflows/ci.yml",
    "docs/phase-7a-experiments.md",
    "pyproject.toml",
)
ATTESTATION_PATH = Path("/usr/local/share/microlens/phase7a-source.json")
_GIT_SHA_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_IGNORED_DIRECTORIES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".vite",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
_IGNORED_FILES = frozenset({".DS_Store"})


def _git_environment() -> dict[str, str]:
    return {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}


def _git_run(
    arguments: list[str],
    *,
    repo_root: Path,
    text: bool = False,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=text,
        env=_git_environment(),
    )


def _included_file(path: Path) -> bool:
    return (
        path.name not in _IGNORED_FILES
        and path.suffix not in {".pyc", ".pyo"}
        and not path.name.endswith(".tsbuildinfo")
    )


def _file_descriptor(path: Path, root: Path) -> dict[str, str]:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ModelInputError(
                f"reviewed source tree contains a symlink: {current.relative_to(root)}"
            )
    if path.is_symlink():
        raise ModelInputError(f"reviewed source tree contains a symlink: {relative}")
    if not path.is_file():
        raise ModelInputError(f"reviewed source file is missing or unsafe: {relative}")
    mode = stat.S_IMODE(path.stat().st_mode)
    return {
        "path": relative.as_posix(),
        "mode": "100755" if mode & stat.S_IXUSR else "100644",
        "sha256": sha256_file(path),
    }


def source_inventory(repo_root: str | Path) -> tuple[dict[str, str], ...]:
    root = Path(repo_root).resolve()
    inventory: list[dict[str, str]] = []
    for root_name in SOURCE_ROOTS:
        source_root = root / root_name
        if source_root.is_symlink() or not source_root.is_dir():
            raise ModelInputError(f"reviewed source root is missing or unsafe: {root_name}")
        for directory, names, filenames in os.walk(source_root, followlinks=False):
            directory_path = Path(directory)
            retained_names: list[str] = []
            for name in sorted(names):
                candidate = directory_path / name
                if name in _IGNORED_DIRECTORIES:
                    continue
                if candidate.is_symlink():
                    raise ModelInputError(
                        f"reviewed source tree contains a symlink: {candidate.relative_to(root)}"
                    )
                retained_names.append(name)
            names[:] = retained_names
            for name in sorted(filenames):
                candidate = directory_path / name
                if not _included_file(candidate):
                    continue
                inventory.append(_file_descriptor(candidate, root))
    for relative in SOURCE_FILES:
        inventory.append(_file_descriptor(root / relative, root))
    return tuple(sorted(inventory, key=lambda row: row["path"]))


def _inventory_checksum(inventory: tuple[dict[str, str], ...]) -> str:
    document = {
        "schema_version": "2.0",
        "roots": list(SOURCE_ROOTS),
        "standalone_files": list(SOURCE_FILES),
        "files": inventory,
    }
    return sha256_bytes(canonical_json_bytes(document))


def source_checksum(repo_root: str | Path) -> str:
    return _inventory_checksum(source_inventory(repo_root))


def _included_commit_path(relative: str) -> bool:
    path = Path(relative)
    if relative in SOURCE_FILES:
        return True
    if not path.parts or path.parts[0] not in SOURCE_ROOTS:
        return False
    if any(part in _IGNORED_DIRECTORIES for part in path.parts[:-1]):
        return False
    return _included_file(path)


def commit_source_inventory(repo_root: str | Path, git_revision: str) -> tuple[dict[str, str], ...]:
    """Read the reviewed executable boundary directly from one Git commit tree."""

    if not _GIT_SHA_PATTERN.fullmatch(git_revision):
        raise ModelInputError("git_revision must be an explicit lowercase 40-character SHA")
    root = Path(repo_root).resolve()
    tree = _git_run(
        [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            git_revision,
            "--",
            *SOURCE_ROOTS,
            *SOURCE_FILES,
        ],
        repo_root=root,
    )
    inventory: list[dict[str, str]] = []
    for raw_entry in tree.stdout.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        relative = raw_path.decode("utf-8")
        if not _included_commit_path(relative):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ModelInputError(f"reviewed commit path is not a regular file: {relative}")
        blob = _git_run(["cat-file", "blob", object_id], repo_root=root).stdout
        inventory.append({"path": relative, "mode": mode, "sha256": sha256_bytes(blob)})
    return tuple(sorted(inventory, key=lambda row: row["path"]))


def validate_reviewed_source(repo_root: str | Path, git_revision: str) -> str:
    """Require current executable bytes to exactly match the requested commit tree."""

    root = Path(repo_root).resolve()
    head = _git_run(["rev-parse", "HEAD"], repo_root=root, text=True).stdout.strip()
    if head != git_revision:
        raise ModelInputError("GIT_REVISION does not match the checked-out repository HEAD")
    status = _git_run(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"],
        repo_root=root,
    ).stdout
    if status:
        raise ModelInputError(
            "formal Phase 7A requires an entirely clean Git worktree with no staged, "
            "unstaged, or non-ignored untracked paths"
        )
    control_paths = _git_run(
        ["ls-tree", "-r", "--name-only", "-z", git_revision, "--", *CONTROL_FILES],
        repo_root=root,
    ).stdout.split(b"\0")
    committed_controls = {path.decode("utf-8") for path in control_paths if path}
    if committed_controls != set(CONTROL_FILES):
        raise ModelInputError("reviewed commit is missing a Phase 7A clean-tree control file")
    for relative in CONTROL_FILES:
        current_control = _file_descriptor(root / relative, root)
        blob = _git_run(["show", f"{git_revision}:{relative}"], repo_root=root).stdout
        committed_mode = _git_run(
            ["ls-tree", git_revision, "--", relative], repo_root=root, text=True
        ).stdout.split(maxsplit=1)[0]
        committed_control = {
            "path": relative,
            "mode": committed_mode,
            "sha256": sha256_bytes(blob),
        }
        if current_control != committed_control:
            raise ModelInputError(
                "reviewed clean-tree control files differ from the requested commit"
            )
    current = source_inventory(root)
    committed = commit_source_inventory(root, git_revision)
    if current != committed:
        raise ModelInputError(
            "reviewed executable source differs from the requested clean Git commit"
        )
    return _inventory_checksum(committed)


def attestation_document(*, git_revision: str, source_checksum_value: str) -> dict[str, Any]:
    if git_revision != "unattested" and not _GIT_SHA_PATTERN.fullmatch(git_revision):
        raise ModelInputError("git_revision must be an explicit lowercase 40-character SHA")
    if not _SHA256_PATTERN.fullmatch(source_checksum_value):
        raise ModelInputError("source checksum must be lowercase SHA-256")
    return {
        "schema_version": "2.0",
        "git_revision": git_revision,
        "source_checksum": source_checksum_value,
        "source_roots": list(SOURCE_ROOTS),
        "source_files": list(SOURCE_FILES),
        "algorithm": "canonical-path-mode-and-content-sha256-v2",
    }


def write_attestation(
    *,
    repo_root: str | Path,
    git_revision: str,
    expected_source_checksum: str,
    output: str | Path,
) -> dict[str, Any]:
    actual = source_checksum(repo_root)
    if expected_source_checksum != "unattested":
        if not _SHA256_PATTERN.fullmatch(expected_source_checksum):
            raise ModelInputError("expected source checksum must be lowercase SHA-256")
        if actual != expected_source_checksum:
            raise ModelInputError(
                "reviewed source checksum does not match the Docker build request"
            )
    document = attestation_document(git_revision=git_revision, source_checksum_value=actual)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json_bytes(document) + b"\n")
    return document


def load_attestation(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ModelInputError("Phase 7A source attestation is missing or unsafe")
    try:
        document = json.loads(target.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelInputError("Phase 7A source attestation is invalid JSON") from exc
    expected_keys = {
        "schema_version",
        "git_revision",
        "source_checksum",
        "source_roots",
        "source_files",
        "algorithm",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise ModelInputError("Phase 7A source attestation has unknown or missing fields")
    if (
        document["schema_version"] != "2.0"
        or document["source_roots"] != list(SOURCE_ROOTS)
        or document["source_files"] != list(SOURCE_FILES)
        or document["algorithm"] != "canonical-path-mode-and-content-sha256-v2"
        or not isinstance(document["git_revision"], str)
        or not _GIT_SHA_PATTERN.fullmatch(document["git_revision"])
        or not isinstance(document["source_checksum"], str)
        or not _SHA256_PATTERN.fullmatch(document["source_checksum"])
    ):
        raise ModelInputError("Phase 7A source attestation is not a formal attestation")
    return document


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Compute and attest reviewed Phase 7A source")
    subparsers = value.add_subparsers(dest="command", required=True)
    compute = subparsers.add_parser("compute")
    compute.add_argument("--repo-root", default=".")
    compute.add_argument("--git-revision")
    attest = subparsers.add_parser("attest")
    attest.add_argument("--repo-root", default=".")
    attest.add_argument("--git-revision", required=True)
    attest.add_argument("--expected-source-checksum", required=True)
    attest.add_argument("--output", default=str(ATTESTATION_PATH))
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "compute":
        checksum = (
            validate_reviewed_source(arguments.repo_root, arguments.git_revision)
            if arguments.git_revision
            else source_checksum(arguments.repo_root)
        )
        print(checksum)
        return 0
    document = write_attestation(
        repo_root=arguments.repo_root,
        git_revision=arguments.git_revision,
        expected_source_checksum=arguments.expected_source_checksum,
        output=arguments.output,
    )
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
