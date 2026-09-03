from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from recsys.data.common import canonical_json_bytes
from recsys.experiments.phase7a import (
    _ensure_namespace,
    _namespace_identity,
    _verify_runtime_identity,
    preflight_phase7a,
    run_phase7a,
)
from recsys.experiments.source_identity import (
    SOURCE_FILES,
    source_checksum,
    write_attestation,
)
from recsys.models.errors import ModelInputError
from scripts.phase7a_launcher import build_docker_argv

from ._support import write_data_version

REPOSITORY = Path(__file__).resolve().parents[2]
GIT_REVISION = "a" * 40
SOURCE_CHECKSUM = "b" * 64
IMAGE_REFERENCE = "registry.example/worker@sha256:" + "c" * 64
RUNTIME_IDENTITY = {
    "git_revision": GIT_REVISION,
    "image_reference": IMAGE_REFERENCE,
    "image_digest": "sha256:" + "c" * 64,
    "source_checksum": SOURCE_CHECKSUM,
    "baked_git_revision": GIT_REVISION,
    "baked_source_checksum": SOURCE_CHECKSUM,
    "recomputed_source_checksum": SOURCE_CHECKSUM,
    "matrix_checksum": "d" * 64,
    "base_config_checksum": "e" * 64,
}


def _source_tree(root: Path) -> None:
    for name in ("apps", "recsys", "configs"):
        target = root / name
        target.mkdir(parents=True)
        (target / f"{name}.txt").write_text(f"{name}\n")
    for relative in SOURCE_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{relative}\n")


def _launcher_environment(processed: Path, run_root: Path) -> dict[str, str]:
    return {
        "DATA_VERSION": "data-v1",
        "DATA_MANIFEST_CHECKSUM": "f" * 64,
        "GIT_REVISION": GIT_REVISION,
        "RUN_ID": "run-1",
        "PHASE7A_IMAGE": IMAGE_REFERENCE,
        "PHASE7A_SOURCE_CHECKSUM": SOURCE_CHECKSUM,
        "PHASE7A_PROCESSED_ROOT": str(processed),
        "PHASE7A_RUN_ROOT": str(run_root),
    }


def test_source_checksum_is_deterministic_and_detects_source_changes(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    first = source_checksum(tmp_path)
    assert first == source_checksum(tmp_path)
    (tmp_path / "apps/apps.txt").write_text("changed\n")
    assert source_checksum(tmp_path) != first
    changed = source_checksum(tmp_path)
    ignored = tmp_path / "apps/node_modules"
    ignored.mkdir()
    (ignored / "ignored.js").write_text("ignored\n")
    assert source_checksum(tmp_path) == changed


def test_source_checksum_rejects_symlinks_in_reviewed_source(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    (tmp_path / "apps/link.py").symlink_to(tmp_path / "apps/apps.txt")
    with pytest.raises(ModelInputError, match="symlink"):
        source_checksum(tmp_path)


def test_runtime_identity_rejects_wrong_sha_source_and_image(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    actual_source = source_checksum(tmp_path)
    attestation = tmp_path / "attestation.json"
    write_attestation(
        repo_root=tmp_path,
        git_revision=GIT_REVISION,
        expected_source_checksum=actual_source,
        output=attestation,
    )
    matrix = SimpleNamespace(matrix_checksum="d" * 64, base_config_checksum="e" * 64)
    with pytest.raises(ModelInputError, match="Git revision"):
        _verify_runtime_identity(
            matrix=matrix,
            repo_root=tmp_path,
            git_revision="0" * 40,
            image_digest=IMAGE_REFERENCE,
            requested_source_checksum=actual_source,
            attestation_path=attestation,
        )
    with pytest.raises(ModelInputError, match="source checksum"):
        _verify_runtime_identity(
            matrix=matrix,
            repo_root=tmp_path,
            git_revision=GIT_REVISION,
            image_digest=IMAGE_REFERENCE,
            requested_source_checksum="0" * 64,
            attestation_path=attestation,
        )
    with pytest.raises(ModelInputError, match="exact name@sha256"):
        _verify_runtime_identity(
            matrix=matrix,
            repo_root=tmp_path,
            git_revision=GIT_REVISION,
            image_digest="worker:latest",
            requested_source_checksum=actual_source,
            attestation_path=attestation,
        )


def test_wrong_baked_attestation_is_rejected_before_output_creation(tmp_path: Path) -> None:
    actual_source = source_checksum(REPOSITORY)
    attestation = tmp_path / "attestation.json"
    write_attestation(
        repo_root=REPOSITORY,
        git_revision="0" * 40,
        expected_source_checksum=actual_source,
        output=attestation,
    )
    output = tmp_path / "must-not-exist"
    with pytest.raises(ModelInputError, match="Git revision"):
        run_phase7a(
            matrix_path="configs/models/experiment-matrix.json",
            repo_root=REPOSITORY,
            processed_root=tmp_path / "processed",
            data_version="data-v1",
            data_manifest_checksum="f" * 64,
            output_root=output,
            run_id="run-1",
            git_revision=GIT_REVISION,
            image_digest=IMAGE_REFERENCE,
            requested_source_checksum=actual_source,
            attestation_path=attestation,
            command=["phase7a", "run"],
        )
    assert not output.exists()


def test_namespace_marker_initialization_and_fresh_root_only_rules(tmp_path: Path) -> None:
    identity = _namespace_identity(
        runtime_identity=RUNTIME_IDENTITY,
        data_version="data-v1",
        data_manifest_checksum="f" * 64,
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _ensure_namespace(empty, identity) == empty
    marker = empty / ".phase7a-namespace.json"
    assert json.loads(marker.read_text()) == identity
    with pytest.raises(ModelInputError, match="already claimed"):
        _ensure_namespace(empty, identity)

    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    (unmarked / "old-run.json").write_text("{}\n")
    with pytest.raises(ModelInputError, match="non-empty unmarked"):
        _ensure_namespace(unmarked, identity)

    incompatible = tmp_path / "incompatible"
    incompatible.mkdir()
    changed = {**identity, "data_version": "different"}
    (incompatible / ".phase7a-namespace.json").write_bytes(canonical_json_bytes(changed) + b"\n")
    with pytest.raises(ModelInputError, match="already claimed"):
        _ensure_namespace(incompatible, identity)


@pytest.mark.parametrize(
    "field",
    [
        "git_revision",
        "image_reference",
        "image_digest",
        "source_checksum",
        "matrix_checksum",
        "base_config_checksum",
        "data_version",
        "data_manifest_checksum",
    ],
)
def test_namespace_marker_is_never_reused_or_overwritten(tmp_path: Path, field: str) -> None:
    identity = _namespace_identity(
        runtime_identity=RUNTIME_IDENTITY,
        data_version="data-v1",
        data_manifest_checksum="f" * 64,
    )
    root = tmp_path / field
    root.mkdir()
    _ensure_namespace(root, identity)
    original = (root / ".phase7a-namespace.json").read_bytes()
    changed = {**identity, field: "changed"}
    with pytest.raises(ModelInputError, match="already claimed"):
        _ensure_namespace(root, changed)
    assert (root / ".phase7a-namespace.json").read_bytes() == original


@pytest.mark.parametrize("compatible", [True, False])
def test_concurrent_namespace_claim_has_exactly_one_winner(
    tmp_path: Path, compatible: bool
) -> None:
    root = tmp_path / "concurrent"
    root.mkdir()
    first = _namespace_identity(
        runtime_identity=RUNTIME_IDENTITY,
        data_version="data-v1",
        data_manifest_checksum="f" * 64,
    )
    second = dict(first if compatible else {**first, "git_revision": "0" * 40})
    barrier = threading.Barrier(2)

    def claim(identity):
        barrier.wait()
        try:
            _ensure_namespace(root, identity)
        except ModelInputError:
            return "REFUSED", identity
        return "PASS", identity

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (first, second)))
    winners = [identity for status, identity in results if status == "PASS"]
    assert len(winners) == 1
    assert [status for status, _identity in results].count("REFUSED") == 1
    assert json.loads((root / ".phase7a-namespace.json").read_bytes()) == winners[0]


@pytest.mark.parametrize("failure", ["write", "file_fsync", "directory_fsync"])
def test_failed_namespace_publish_leaves_no_partial_marker(tmp_path: Path, failure: str) -> None:
    root = tmp_path / failure
    root.mkdir()
    identity = _namespace_identity(
        runtime_identity=RUNTIME_IDENTITY,
        data_version="data-v1",
        data_manifest_checksum="f" * 64,
    )
    if failure == "write":
        patcher = mock.patch("recsys.experiments.phase7a.os.write", side_effect=OSError("write"))
    elif failure == "file_fsync":
        patcher = mock.patch("recsys.experiments.phase7a.os.fsync", side_effect=OSError("fsync"))
    else:
        patcher = mock.patch(
            "recsys.experiments.phase7a.fsync_directory", side_effect=[OSError("publish"), None]
        )
    with patcher, pytest.raises(OSError):
        _ensure_namespace(root, identity)
    assert list(root.iterdir()) == []


def test_direct_launcher_uses_one_shared_strict_docker_envelope(tmp_path: Path) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    run_root = tmp_path / "fresh-run"
    environment = _launcher_environment(processed, run_root)
    with (
        mock.patch(
            "scripts.phase7a_launcher.validate_reviewed_source", return_value=SOURCE_CHECKSUM
        ),
    ):
        run_argv, returned_root = build_docker_argv(
            mode="run", environment=environment, repo_root=tmp_path
        )
    assert returned_root == run_root
    assert run_argv[:2] == ["docker", "run"]
    for flag in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--memory=5g",
        "--memory-swap=5g",
        "--cpus=4",
        "--pids-limit=512",
    ):
        assert run_argv.count(flag) == 1
    assert "/tmp:rw,noexec,nosuid,nodev,size=256m" in run_argv
    assert run_argv.count("--mount") == 2
    mounts = [run_argv[index + 1] for index, token in enumerate(run_argv) if token == "--mount"]
    assert sum(value.endswith(",readonly") for value in mounts) == 1
    assert sum(not value.endswith(",readonly") for value in mounts) == 1
    assert IMAGE_REFERENCE in run_argv
    assert not {"compose", "worker", "--env-file"} & set(run_argv)

    probe_root = tmp_path / "probe-output"
    probe_root.mkdir()
    probe_environment = {**environment, "PHASE7A_RUN_ROOT": str(probe_root)}
    with (
        mock.patch(
            "scripts.phase7a_launcher.validate_reviewed_source", return_value=SOURCE_CHECKSUM
        ),
    ):
        preflight_argv, _ = build_docker_argv(
            mode="preflight", environment=probe_environment, repo_root=tmp_path
        )
    first_mount = run_argv.index("--mount")
    assert preflight_argv[:first_mount] == run_argv[:first_mount]
    assert preflight_argv[run_argv.index(IMAGE_REFERENCE)] == IMAGE_REFERENCE
    assert preflight_argv.count("--mount") == run_argv.count("--mount") == 2
    assert "preflight" in preflight_argv
    assert list(probe_root.iterdir()) == []


def test_launcher_rejects_mutable_image_existing_and_symlink_roots(tmp_path: Path) -> None:
    processed = tmp_path / "processed"
    processed.mkdir()
    run_root = tmp_path / "fresh"
    environment = _launcher_environment(processed, run_root)
    with pytest.raises(ModelInputError, match="exact name@sha256"):
        build_docker_argv(
            mode="run",
            environment={**environment, "PHASE7A_IMAGE": "worker:latest"},
            repo_root=tmp_path,
        )

    historical = tmp_path / "output/phase7a"
    historical.mkdir(parents=True)
    with pytest.raises(ModelInputError, match="existing run root"):
        build_docker_argv(
            mode="preflight",
            environment={**environment, "PHASE7A_RUN_ROOT": str(historical)},
            repo_root=tmp_path,
        )
    symlink = tmp_path / "probe-link"
    symlink.symlink_to(historical, target_is_directory=True)
    with pytest.raises(ModelInputError, match="symlink"):
        build_docker_argv(
            mode="preflight",
            environment={**environment, "PHASE7A_RUN_ROOT": str(symlink)},
            repo_root=tmp_path,
        )


def test_preflight_writes_nothing_and_does_not_open_processed_data(tmp_path: Path) -> None:
    matrix = SimpleNamespace(matrix_checksum="d" * 64, base_config_checksum="e" * 64)
    with (
        mock.patch("recsys.experiments.phase7a.resolve_matrix", return_value=matrix),
        mock.patch(
            "recsys.experiments.phase7a._verify_runtime_identity",
            return_value=RUNTIME_IDENTITY,
        ),
        mock.patch("recsys.experiments.phase7a._runtime_envelope", return_value={"verified": True}),
        mock.patch("pathlib.Path.open", side_effect=AssertionError("data must not be opened")),
    ):
        result = preflight_phase7a(
            matrix_path="/workspace/configs/models/experiment-matrix.json",
            repo_root="/workspace",
            processed_root="/artifacts/processed",
            data_version="probe-no-data",
            data_manifest_checksum="0" * 64,
            output_root="/phase7a",
            run_id="probe-no-data",
            git_revision=GIT_REVISION,
            image_digest=IMAGE_REFERENCE,
            requested_source_checksum=SOURCE_CHECKSUM,
            attestation_path="/attestation.json",
        )
    assert result["data_read"] is False
    assert result["output_written"] is False


def test_wrong_data_manifest_checksum_fails_before_namespace_and_training(tmp_path: Path) -> None:
    version, _checksum = write_data_version(tmp_path / "processed")
    output = tmp_path / "must-not-exist"
    with (
        mock.patch(
            "recsys.experiments.phase7a._verify_runtime_identity", return_value=RUNTIME_IDENTITY
        ),
        mock.patch("recsys.experiments.phase7a._runtime_envelope") as envelope,
        mock.patch("recsys.models.entrypoint.train_model_stages") as train,
        mock.patch("recsys.models.entrypoint.finalize_trained_model") as finalize,
        pytest.raises(ModelInputError, match="data manifest checksum mismatch"),
    ):
        run_phase7a(
            matrix_path="configs/models/experiment-matrix.json",
            repo_root=REPOSITORY,
            processed_root=tmp_path / "processed",
            data_version=version,
            data_manifest_checksum="0" * 64,
            output_root=output,
            run_id="wrong-data",
            git_revision=GIT_REVISION,
            image_digest=IMAGE_REFERENCE,
            requested_source_checksum=SOURCE_CHECKSUM,
            attestation_path=tmp_path / "attestation.json",
            command=["phase7a", "run"],
        )
    envelope.assert_not_called()
    train.assert_not_called()
    finalize.assert_not_called()
    assert not output.exists()


def test_makefile_and_worker_image_expose_only_the_attested_direct_launcher() -> None:
    makefile = (REPOSITORY / "Makefile").read_text()
    phase7a = makefile[makefile.index("phase7a-run:") : makefile.index("train-sync:")]
    assert "scripts/phase7a_launcher.py run" in phase7a
    assert "scripts/phase7a_launcher.py preflight" in phase7a
    assert "scripts/phase7a_launcher.py build" in makefile
    assert "docker compose" not in phase7a
    assert "$(DOCKER_COMPOSE)" not in phase7a
    assert "PHASE7A_OUTPUT_ROOT" not in phase7a
    dockerfile = (REPOSITORY / "apps/worker/Dockerfile").read_text()
    assert "ARG GIT_REVISION" in dockerfile
    assert "ARG PHASE7A_SOURCE_CHECKSUM" in dockerfile
    assert "phase7a-source.json" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
