#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import re
import resource
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_PROBE_SLOTS = 2_000_000
DEFAULT_USER_COUNT = 10_000
DEFAULT_TOP_N = 200
DEFAULT_SLOT_ALLOWANCE_BYTES = 128
ALGORITHM = "shared-catalog-three-retained-structures-v1"
_IMAGE_REFERENCE_PATTERN = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")
_IMAGE_ID_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_CAPTURE_KEYS = {
    "captured_at_utc",
    "docker_argv",
    "image_id",
    "image_reference",
    "probe_sha256",
}
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_FIXED_DOCKER_TOKENS = (
    "--rm",
    "--pull=never",
    "--network=none",
    "--read-only",
    "--memory=512m",
    "--memory-swap=512m",
    "--cpus=4",
    "--pids-limit=128",
)
_TMPFS_SPEC = "/tmp:rw,noexec,nosuid,nodev,size=64m"
_CONTAINER_COMMAND = (
    "python",
    "/probe.py",
    "--capture-metadata",
    "/probe-capture.json",
)


def _rss_sample() -> tuple[int, int, str]:
    raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return raw, raw, "bytes"
    return raw, raw * 1024, "kibibytes"


def validate_capture_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != _CAPTURE_KEYS:
        raise ValueError("capture metadata has unknown or missing fields")
    timestamp = value["captured_at_utc"]
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("capture timestamp must be an explicit UTC timestamp")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("capture timestamp must be an explicit UTC timestamp") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() != UTC.utcoffset(
        parsed_timestamp
    ):
        raise ValueError("capture timestamp must be an explicit UTC timestamp")

    image_reference = value["image_reference"]
    image_id = value["image_id"]
    if not isinstance(image_reference, str) or not _IMAGE_REFERENCE_PATTERN.fullmatch(
        image_reference
    ):
        raise ValueError("capture image_reference must be immutable name@sha256")
    if image_id is not None and (
        not isinstance(image_id, str) or not _IMAGE_ID_PATTERN.fullmatch(image_id)
    ):
        raise ValueError("capture image_id must be null or sha256:<64>")
    probe_sha256 = value["probe_sha256"]
    if not isinstance(probe_sha256, str) or not _SHA256_PATTERN.fullmatch(probe_sha256):
        raise ValueError("capture probe_sha256 must be 64 lowercase hex characters")

    raw_argv = value["docker_argv"]
    if (
        not isinstance(raw_argv, list)
        or not raw_argv
        or any(not isinstance(token, str) or not token for token in raw_argv)
    ):
        raise ValueError("capture docker_argv must be a non-empty token array")
    docker_argv = list(raw_argv)
    fixed_prefix = [
        "docker",
        "run",
        *_FIXED_DOCKER_TOKENS,
        "--tmpfs",
        _TMPFS_SPEC,
    ]
    expected_length = len(fixed_prefix) + 2 + 2 + 1 + len(_CONTAINER_COMMAND)
    if len(docker_argv) != expected_length or docker_argv[: len(fixed_prefix)] != fixed_prefix:
        raise ValueError("capture docker_argv does not match the complete ordered Docker grammar")

    cursor = len(fixed_prefix)
    mounts: list[str] = []
    expected_destinations = ("/probe.py", "/probe-capture.json")
    for expected_destination in expected_destinations:
        if docker_argv[cursor] != "--mount":
            raise ValueError(
                "capture docker_argv does not match the complete ordered Docker grammar"
            )
        mount = docker_argv[cursor + 1]
        parts = mount.split(",")
        parsed_parts = [part.split("=", 1) if "=" in part else [part, ""] for part in parts]
        field_names = [part[0] for part in parsed_parts]
        fields = dict(parsed_parts)
        if (
            field_names != ["type", "src", "dst", "readonly"]
            or len(fields) != 4
            or set(fields) != {"type", "src", "dst", "readonly"}
            or fields["type"] != "bind"
            or not fields["src"]
            or not Path(fields["src"]).is_absolute()
            or fields["dst"] != expected_destination
            or fields["readonly"] != ""
            or parts[-1] != "readonly"
        ):
            raise ValueError("capture docker_argv mount must use the exact read-only bind grammar")
        mounts.append(mount)
        cursor += 2

    if docker_argv[cursor] != image_reference:
        raise ValueError("capture docker_argv does not bind the immutable image reference")
    if tuple(docker_argv[cursor + 1 :]) != _CONTAINER_COMMAND:
        raise ValueError("capture docker_argv has an unexpected probe command")
    return {
        "captured_at_utc": timestamp,
        "image_reference": image_reference,
        "image_id": image_id,
        "probe_sha256": probe_sha256,
        "docker_argv": docker_argv,
        "container_envelope": {
            "pull_policy": "never",
            "network": "none",
            "root_filesystem_read_only": True,
            "memory_limit_bytes": 512 * 1024**2,
            "memory_swap_limit_bytes": 512 * 1024**2,
            "cpu_limit": 4,
            "pids_limit": 128,
            "tmpfs": _TMPFS_SPEC,
            "bind_mounts": mounts,
        },
    }


def load_capture_metadata(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("capture metadata path must be a real file")
    try:
        value = json.loads(target.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("capture metadata must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("capture metadata must be a JSON object")
    validate_capture_metadata(value)
    actual_probe_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if value["probe_sha256"] != actual_probe_sha256:
        raise ValueError("capture probe_sha256 does not match the executing probe bytes")
    return value


def probe_candidate_rss(
    *,
    user_count: int,
    top_n: int,
    slot_allowance_bytes: int,
    capture_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    slots = user_count * top_n
    if user_count < 1 or top_n < 1 or slot_allowance_bytes < 1:
        raise ValueError("probe dimensions and allowance must be positive")
    if slots > MAX_PROBE_SLOTS:
        raise ValueError(f"probe is bounded to at most {MAX_PROBE_SLOTS} retained slots")

    item_ids = tuple(f"item-{index:06d}" for index in range(top_n))
    gc.collect()
    baseline_raw, baseline_bytes, raw_unit = _rss_sample()
    candidate_rankings: dict[str, list[str]] = {}
    candidate_scores: dict[str, dict[str, float]] = {}
    reranked: dict[str, list[str]] = {}
    for user_index in range(user_count):
        user_id = f"user-{user_index:06d}"
        candidate_rankings[user_id] = list(item_ids)
        candidate_scores[user_id] = {
            item_id: float(item_index) for item_index, item_id in enumerate(item_ids)
        }
        reranked[user_id] = list(item_ids)
    gc.collect()
    peak_raw, peak_bytes, _peak_unit = _rss_sample()

    retained_counts = {
        "candidate_users": len(candidate_rankings),
        "score_users": len(candidate_scores),
        "reranked_users": len(reranked),
        "candidate_slots": sum(len(rows) for rows in candidate_rankings.values()),
        "score_slots": sum(len(rows) for rows in candidate_scores.values()),
        "reranked_slots": sum(len(rows) for rows in reranked.values()),
    }
    expected_slots = slots
    if retained_counts != {
        "candidate_users": user_count,
        "score_users": user_count,
        "reranked_users": user_count,
        "candidate_slots": expected_slots,
        "score_slots": expected_slots,
        "reranked_slots": expected_slots,
    }:
        raise RuntimeError("candidate RSS probe did not retain the requested shape")

    delta_bytes = max(0, peak_bytes - baseline_bytes)
    bytes_per_slot = delta_bytes / slots
    result = {
        "schema_version": "2.0",
        "kind": "focused_candidate_structure_rss_probe",
        "capture": validate_capture_metadata(capture_metadata),
        "algorithm": {
            "id": ALGORITHM,
            "catalog": "one tuple of shared item-id strings",
            "candidate_rankings": "dict[user_id] -> list[shared item-id reference]",
            "candidate_scores": "dict[user_id] -> dict[shared item-id -> unique float]",
            "reranked": "dict[user_id] -> list[shared item-id reference]",
            "rss_measure": "resource.getrusage(RUSAGE_SELF).ru_maxrss process peak",
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "pointer_bits": 64 if sys.maxsize > 2**32 else 32,
        },
        "parameters": {
            "user_count": user_count,
            "top_n": top_n,
            "retained_slots": slots,
            "slot_allowance_bytes": slot_allowance_bytes,
            "maximum_probe_slots": MAX_PROBE_SLOTS,
        },
        "retained_counts": retained_counts,
        "raw_rss": {
            "unit": raw_unit,
            "baseline": baseline_raw,
            "peak": peak_raw,
        },
        "result": {
            "baseline_rss_bytes": baseline_bytes,
            "peak_rss_bytes": peak_bytes,
            "delta_rss_bytes": delta_bytes,
            "bytes_per_slot": bytes_per_slot,
            "within_slot_allowance": bytes_per_slot <= slot_allowance_bytes,
        },
        "scope": "focused retained-structure evidence; not a full-data or model-process result",
    }
    if not result["result"]["within_slot_allowance"]:
        raise MemoryError(
            f"measured {bytes_per_slot:.6f} bytes/slot exceeds {slot_allowance_bytes}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Phase 7A candidate-structure RSS probe")
    parser.add_argument("--user-count", type=int, default=DEFAULT_USER_COUNT)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--slot-allowance-bytes", type=int, default=DEFAULT_SLOT_ALLOWANCE_BYTES)
    parser.add_argument("--capture-metadata", required=True)
    arguments = parser.parse_args()
    result = probe_candidate_rss(
        user_count=arguments.user_count,
        top_n=arguments.top_n,
        slot_allowance_bytes=arguments.slot_allowance_bytes,
        capture_metadata=load_capture_metadata(arguments.capture_metadata),
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
