from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .contracts import AnalyticsContractError

SCHEMA_VERSION = "1.0"
CONTRACT_METADATA = {b"microlens.analytics.contract": b"hive-compatible-v1"}


@dataclass(frozen=True, slots=True)
class FieldContract:
    name: str
    logical_type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class DatasetContract:
    name: str
    version: str
    fields: tuple[FieldContract, ...]
    partition_fields: tuple[str, ...]

    def canonical_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "fields": [
                {"name": field.name, "logical_type": field.logical_type, "nullable": field.nullable}
                for field in self.fields
            ],
            "partition_fields": list(self.partition_fields),
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


EVENTS_CONTRACT = DatasetContract(
    name="events",
    version=SCHEMA_VERSION,
    fields=(
        FieldContract("event_sequence_id", "bigint", False),
        FieldContract("event_id", "string", False),
        FieldContract("exposure_id", "string", False),
        FieldContract("request_id", "string", False),
        FieldContract("user_id", "string", False),
        FieldContract("item_id", "string", False),
        FieldContract("position", "int", False),
        FieldContract("feed_type", "string", False),
        FieldContract("source", "string", False),
        FieldContract("event_type", "string", False),
        FieldContract("client_timestamp", "timestamp_utc_ms", True),
        FieldContract("server_timestamp", "timestamp_utc_ms", False),
        FieldContract("duration_ms", "bigint", True),
        FieldContract("payload_json", "string", False),
        FieldContract("schema_version", "string", False),
        FieldContract("dt", "date_string", False),
    ),
    partition_fields=("dt", "event_type"),
)

EXPOSURES_CONTRACT = DatasetContract(
    name="exposures",
    version=SCHEMA_VERSION,
    fields=(
        FieldContract("canonical_event_sequence_id", "bigint", False),
        FieldContract("exposure_id", "string", False),
        FieldContract("request_id", "string", False),
        FieldContract("snapshot_id", "string", False),
        FieldContract("user_id", "string", False),
        FieldContract("item_id", "string", False),
        FieldContract("position", "int", False),
        FieldContract("feed_type", "string", False),
        FieldContract("source", "string", False),
        FieldContract("model_version", "string", False),
        FieldContract("exposed_at", "timestamp_utc_ms", False),
        FieldContract("schema_version", "string", False),
        FieldContract("dt", "date_string", False),
    ),
    partition_fields=("dt", "feed_type"),
)


def validate_additive_evolution(old: DatasetContract, new: DatasetContract) -> None:
    """Permit only same-major, nullable, append-only field additions."""

    try:
        old_major = int(old.version.split(".", maxsplit=1)[0])
        new_major = int(new.version.split(".", maxsplit=1)[0])
    except (ValueError, IndexError) as exc:
        raise AnalyticsContractError("schema versions must be numeric major.minor") from exc
    if old.name != new.name or old_major != new_major:
        raise AnalyticsContractError("dataset name and schema major version must remain stable")
    if old.partition_fields != new.partition_fields:
        raise AnalyticsContractError("partition fields cannot change within a schema major")
    if len(new.fields) < len(old.fields):
        raise AnalyticsContractError("schema evolution cannot remove fields")
    if new.fields[: len(old.fields)] != old.fields:
        raise AnalyticsContractError("existing fields, order, types and nullability cannot change")
    if any(not field.nullable for field in new.fields[len(old.fields) :]):
        raise AnalyticsContractError("new fields must be nullable within a schema major")


def pyarrow_schemas(*, allow_unsupported_version: bool = False):
    try:
        import pyarrow as pa
    except ImportError as exc:
        raise RuntimeError("analytics export requires requirements-data.lock") from exc
    if pa.__version__ != "25.0.1" and not allow_unsupported_version:
        raise RuntimeError(
            f"analytics export requires exact pyarrow==25.0.1; found {pa.__version__}"
        )
    timestamp = pa.timestamp("ms", tz="UTC")
    events_physical = pa.schema(
        [
            pa.field("event_sequence_id", pa.int64(), nullable=False),
            pa.field("event_id", pa.string(), nullable=False),
            pa.field("exposure_id", pa.string(), nullable=False),
            pa.field("request_id", pa.string(), nullable=False),
            pa.field("user_id", pa.string(), nullable=False),
            pa.field("item_id", pa.string(), nullable=False),
            pa.field("position", pa.int32(), nullable=False),
            pa.field("feed_type", pa.string(), nullable=False),
            pa.field("source", pa.string(), nullable=False),
            pa.field("client_timestamp", timestamp),
            pa.field("server_timestamp", timestamp, nullable=False),
            pa.field("duration_ms", pa.int64()),
            pa.field("payload_json", pa.string(), nullable=False),
            pa.field("schema_version", pa.string(), nullable=False),
        ],
        metadata=CONTRACT_METADATA,
    )
    exposures_physical = pa.schema(
        [
            pa.field("canonical_event_sequence_id", pa.int64(), nullable=False),
            pa.field("exposure_id", pa.string(), nullable=False),
            pa.field("request_id", pa.string(), nullable=False),
            pa.field("snapshot_id", pa.string(), nullable=False),
            pa.field("user_id", pa.string(), nullable=False),
            pa.field("item_id", pa.string(), nullable=False),
            pa.field("position", pa.int32(), nullable=False),
            pa.field("source", pa.string(), nullable=False),
            pa.field("model_version", pa.string(), nullable=False),
            pa.field("exposed_at", timestamp, nullable=False),
            pa.field("schema_version", pa.string(), nullable=False),
        ],
        metadata=CONTRACT_METADATA,
    )
    return pa, {"events": events_physical, "exposures": exposures_physical}
