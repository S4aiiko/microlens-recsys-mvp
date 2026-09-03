from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.phase7a_candidate_rss_probe import (
    ALGORITHM,
    load_capture_metadata,
    probe_candidate_rss,
    validate_capture_metadata,
)

IMAGE_REFERENCE = "python@sha256:" + "a" * 64
PROBE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "phase7a_candidate_rss_probe.py"
PROBE_SHA256 = hashlib.sha256(PROBE_PATH.read_bytes()).hexdigest()


def capture_metadata(*, probe_sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "captured_at_utc": "2026-09-03T16:45:00Z",
        "image_reference": IMAGE_REFERENCE,
        "image_id": "sha256:" + "a" * 64,
        "probe_sha256": probe_sha256,
        "docker_argv": [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=4",
            "--pids-limit=128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            "type=bind,src=/host/probe.py,dst=/probe.py,readonly",
            "--mount",
            "type=bind,src=/host/capture.json,dst=/probe-capture.json,readonly",
            IMAGE_REFERENCE,
            "python",
            "/probe.py",
            "--capture-metadata",
            "/probe-capture.json",
        ],
    }


def hostile_docker_argv(case: str) -> list[str]:
    argv = list(capture_metadata()["docker_argv"])
    image_index = argv.index(IMAGE_REFERENCE)
    if case == "network_conflict":
        argv[image_index:image_index] = ["--network=host"]
    elif case == "network_split":
        argv[image_index:image_index] = ["--network", "host"]
    elif case == "memory_conflict":
        argv[image_index:image_index] = ["--memory=2g"]
    elif case == "memory_split":
        argv[image_index:image_index] = ["--memory", "2g"]
    elif case == "privileged":
        argv[image_index:image_index] = ["--privileged"]
    elif case == "volume_alias":
        argv[image_index:image_index] = ["-v", "/host:/extra:ro"]
    elif case == "extra_mount":
        argv[image_index:image_index] = [
            "--mount",
            "type=bind,src=/host/extra,dst=/extra,readonly",
        ]
    elif case == "duplicate_option":
        argv[image_index:image_index] = ["--rm"]
    elif case == "unexpected_token":
        argv[image_index:image_index] = ["--label=unsafe"]
    elif case == "post_image_command_drift":
        argv.append("--unexpected")
    else:
        first_mount = argv.index("--mount") + 1
        mount = argv[first_mount]
        if case == "duplicate_mount_field":
            argv[first_mount] = mount + ",src=/host/other"
        elif case == "unknown_mount_field":
            argv[first_mount] = mount + ",bind-propagation=rprivate"
        elif case == "readonly_false":
            argv[first_mount] = mount.replace(",readonly", ",readonly=false")
        elif case == "readonly_true":
            argv[first_mount] = mount.replace(",readonly", ",readonly=true")
        else:
            raise AssertionError(f"unknown hostile argv case: {case}")
    return argv


def test_candidate_rss_probe_preserves_bounded_reviewed_shape_and_capture() -> None:
    result = probe_candidate_rss(
        user_count=20,
        top_n=10,
        slot_allowance_bytes=1_000_000,
        capture_metadata=capture_metadata(),
    )
    assert result["schema_version"] == "2.0"
    assert result["algorithm"]["id"] == ALGORITHM
    assert result["capture"]["image_reference"] == IMAGE_REFERENCE
    assert result["capture"]["container_envelope"]["memory_limit_bytes"] == 512 * 1024**2
    assert result["parameters"]["retained_slots"] == 200
    assert result["retained_counts"] == {
        "candidate_users": 20,
        "score_users": 20,
        "reranked_users": 20,
        "candidate_slots": 200,
        "score_slots": 200,
        "reranked_slots": 200,
    }
    assert result["result"]["within_slot_allowance"] is True
    assert result["scope"].startswith("focused retained-structure evidence")


def test_loaded_capture_metadata_can_be_passed_to_probe(tmp_path) -> None:
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(
        json.dumps(capture_metadata(probe_sha256=PROBE_SHA256)), encoding="utf-8"
    )

    loaded = load_capture_metadata(capture_path)
    result = probe_candidate_rss(
        user_count=2,
        top_n=3,
        slot_allowance_bytes=1_000_000,
        capture_metadata=loaded,
    )

    assert set(loaded) == {
        "captured_at_utc",
        "docker_argv",
        "image_id",
        "image_reference",
        "probe_sha256",
    }
    assert result["capture"]["container_envelope"]["pull_policy"] == "never"


def test_loaded_capture_metadata_rejects_wrong_executing_probe_checksum(tmp_path) -> None:
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(capture_metadata()), encoding="utf-8")

    with pytest.raises(ValueError, match="executing probe bytes"):
        load_capture_metadata(capture_path)


def test_candidate_rss_probe_refuses_unbounded_or_invalid_dimensions() -> None:
    with pytest.raises(ValueError, match="at most 2000000"):
        probe_candidate_rss(
            user_count=10_001,
            top_n=200,
            slot_allowance_bytes=128,
            capture_metadata=capture_metadata(),
        )
    with pytest.raises(ValueError, match="positive"):
        probe_candidate_rss(
            user_count=0,
            top_n=200,
            slot_allowance_bytes=128,
            capture_metadata=capture_metadata(),
        )


@pytest.mark.parametrize(
    "mutation,error",
    [
        ({"image_reference": "python:3.12-slim"}, "immutable"),
        ({"captured_at_utc": "not-a-time"}, "UTC timestamp"),
        ({"probe_sha256": "not-a-checksum"}, "64 lowercase hex"),
        ({"docker_argv": ["docker", "run"]}, "complete ordered Docker grammar"),
    ],
)
def test_candidate_rss_capture_metadata_fails_closed(
    mutation: dict[str, object], error: str
) -> None:
    value = {**capture_metadata(), **mutation}
    with pytest.raises(ValueError, match=error):
        validate_capture_metadata(value)


@pytest.mark.parametrize(
    "case",
    [
        "network_conflict",
        "network_split",
        "memory_conflict",
        "memory_split",
        "privileged",
        "volume_alias",
        "extra_mount",
        "duplicate_option",
        "duplicate_mount_field",
        "unknown_mount_field",
        "readonly_false",
        "readonly_true",
        "unexpected_token",
        "post_image_command_drift",
    ],
)
def test_candidate_rss_capture_rejects_hostile_docker_argv(case: str) -> None:
    value = {**capture_metadata(), "docker_argv": hostile_docker_argv(case)}

    with pytest.raises(ValueError, match="Docker grammar|mount|probe command"):
        validate_capture_metadata(value)
