from __future__ import annotations

from datetime import datetime
from typing import Protocol

from apps.api.app.async_runtime.domain import DurableClaim
from apps.api.app.search.domain import FullReindexSpec, IncrementalIndexSpec


class FullReindexRunner(Protocol):
    def run(self, spec: FullReindexSpec): ...


class IncrementalIndexRunner(Protocol):
    def run(self, spec: IncrementalIndexSpec): ...


class FullReindexTaskHandler:
    task_name = "search.full_reindex"

    def __init__(self, runner: FullReindexRunner) -> None:
        self.runner = runner

    def handle(self, claim: DurableClaim, *, now: datetime) -> dict[str, object]:
        del now
        payload = _strict_payload(
            claim.job.payload,
            required={"index_version", "source_version", "batch_size"},
            optional={"expected_current_index"},
        )
        result = self.runner.run(
            FullReindexSpec(
                index_version=_string(payload, "index_version"),
                source_version=_string(payload, "source_version"),
                expected_current_index=_optional_string(payload, "expected_current_index"),
                batch_size=_integer(payload, "batch_size"),
            )
        )
        return {
            "physical_index": result.physical_index,
            "previous_index": result.previous_index,
            "document_count": result.document_count,
            "projection_checksum": result.projection_checksum,
            "replayed": result.replayed,
        }


class IncrementalIndexTaskHandler:
    task_name = "search.incremental_index"

    def __init__(self, runner: IncrementalIndexRunner) -> None:
        self.runner = runner

    def handle(self, claim: DurableClaim, *, now: datetime) -> dict[str, object]:
        del now
        payload = _strict_payload(
            claim.job.payload,
            required={"task_key", "item_ids", "source_watermark", "refresh"},
            optional=set(),
        )
        item_ids = payload["item_ids"]
        if not isinstance(item_ids, list) or any(not isinstance(value, str) for value in item_ids):
            raise ValueError("item_ids must be a JSON array of strings")
        refresh = payload["refresh"]
        if not isinstance(refresh, bool):
            raise ValueError("refresh must be a boolean")
        result = self.runner.run(
            IncrementalIndexSpec(
                task_key=_string(payload, "task_key"),
                item_ids=tuple(item_ids),
                source_watermark=_string(payload, "source_watermark"),
                refresh=refresh,
            )
        )
        return {
            "physical_index": result.physical_index,
            "upserted": result.upserted,
            "deleted": result.deleted,
            "source_watermark": result.source_watermark,
            "replayed": result.replayed,
        }


def _strict_payload(
    payload: dict[str, object], *, required: set[str], optional: set[str]
) -> dict[str, object]:
    keys = set(payload)
    if keys - required - optional:
        raise ValueError(f"unexpected search task fields: {sorted(keys - required - optional)}")
    missing = required - keys
    if missing:
        raise ValueError(f"missing search task fields: {sorted(missing)}")
    return payload


def _string(payload: dict[str, object], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_string(payload: dict[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value
