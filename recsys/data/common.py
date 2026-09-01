from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON deterministically for content addressing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return json.loads(canonical_json_bytes(value))
    path = Path(value)
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object: {path}")
    return parsed


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"timestamp must be UTC with Z suffix: {value!r}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"timestamp must be UTC: {value!r}")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    value = value.astimezone(UTC)
    if value.microsecond:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch_ms_to_utc(value: int) -> str:
    return format_utc(datetime.fromtimestamp(value / 1000, tz=UTC))


def utc_to_epoch_ms(value: str) -> int:
    return math.floor(parse_utc(value).timestamp() * 1000)


def artifact_descriptor(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "path": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if rows is not None:
        descriptor["rows"] = rows
    return descriptor


def validate_relative_file_name(value: Any) -> str:
    """Return one safe relative file name or raise ValueError."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("path must be one non-empty relative file name")
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 1 or value in {".", ".."}:
        raise ValueError("path must be one non-empty relative file name")
    return value


def validate_artifact_descriptor(value: Any, *, require_rows: bool = False) -> dict[str, Any]:
    """Validate the frozen fileArtifact shape without following its path."""

    if not isinstance(value, dict):
        raise ValueError("artifact descriptor must be an object")
    allowed = {"path", "size_bytes", "sha256", "rows"}
    required = {"path", "size_bytes", "sha256"}
    if require_rows:
        required.add("rows")
    if missing := required - set(value):
        raise ValueError(f"artifact descriptor missing fields: {sorted(missing)}")
    if extra := set(value) - allowed:
        raise ValueError(f"artifact descriptor has unknown fields: {sorted(extra)}")
    path = validate_relative_file_name(value["path"])
    size_bytes = value["size_bytes"]
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError("artifact size_bytes must be a non-negative integer")
    checksum = value["sha256"]
    if not isinstance(checksum, str) or not SHA256_PATTERN.fullmatch(checksum):
        raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")
    if "rows" in value:
        rows = value["rows"]
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise ValueError("artifact rows must be a non-negative integer")
    return {**value, "path": path}


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
