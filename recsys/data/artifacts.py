from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, fsync_file


class TableCodec(ABC):
    """Storage boundary kept separate from data transformations."""

    suffix: str
    format_name: str

    @abstractmethod
    def write_rows(self, path: Path, rows: Iterable[dict[str, Any]]) -> int:
        """Write rows deterministically and return the row count."""

    @abstractmethod
    def read_rows(self, path: Path) -> list[dict[str, Any]]:
        """Read rows from an artifact."""


class JsonLinesCodec(TableCodec):
    """Dependency-free fixture codec; never represented as Parquet."""

    suffix = ".jsonl"
    format_name = "canonical_json_lines_test_fixture"

    def write_rows(self, path: Path, rows: Iterable[dict[str, Any]]) -> int:
        count = 0
        with path.open("wb") as handle:
            for row in rows:
                handle.write(canonical_json_bytes(row))
                handle.write(b"\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        return count

    def read_rows(self, path: Path) -> list[dict[str, Any]]:
        import json

        parsed: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{line_number}: expected JSON object")
                parsed.append(row)
        return parsed


class ParquetCodec(TableCodec):
    """Frozen deterministic Parquet writer for public data artifacts."""

    suffix = ".parquet"
    expected_pyarrow_version = "25.0.1"
    base_format_name = "parquet_pyarrow_25_0_1_v1"
    row_group_size = 65_536
    compression = "zstd"
    compression_level = 3

    def __init__(self, *, allow_unsupported_version: bool = False) -> None:
        self.allow_unsupported_version = allow_unsupported_version

    @property
    def format_name(self) -> str:
        try:
            from importlib.metadata import version

            installed = version("pyarrow")
        except Exception:
            return self.base_format_name
        if installed == self.expected_pyarrow_version:
            return self.base_format_name
        if self.allow_unsupported_version:
            return f"parquet_test_fixture_pyarrow_{installed.replace('.', '_')}"
        return self.base_format_name

    def _arrow(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError(
                "ParquetCodec requires the approved exact data dependency; "
                "install requirements-data.lock"
            ) from exc
        if pa.__version__ != self.expected_pyarrow_version and not self.allow_unsupported_version:
            raise RuntimeError(
                f"ParquetCodec requires exact pyarrow=={self.expected_pyarrow_version}; "
                f"found {pa.__version__}"
            )
        return pa, pq

    def validate_runtime(self) -> None:
        """Fail before any public build work if the exact writer is unavailable."""

        self._arrow()

    def schemas(self):
        pa, _ = self._arrow()
        string_list = pa.list_(pa.field("element", pa.string()))
        timestamp_list = pa.list_(pa.field("element", pa.int64()))
        metadata = {b"recsys_table_contract": b"phase-2a-v1"}
        interaction = pa.schema(
            [
                pa.field("user_id", pa.string(), nullable=False),
                pa.field("item_id", pa.string(), nullable=False),
                pa.field("timestamp", pa.int64(), nullable=False),
            ],
            metadata=metadata,
        )
        return {
            "items": pa.schema(
                [
                    pa.field("item_id", pa.string(), nullable=False),
                    pa.field("title", pa.string(), nullable=False),
                    pa.field("likes_snapshot", pa.int64(), nullable=False),
                    pa.field("views_snapshot", pa.int64(), nullable=False),
                    pa.field("cover_ref", pa.string()),
                    pa.field("metadata_status", pa.string(), nullable=False),
                ],
                metadata=metadata,
            ),
            "train": interaction,
            "validation": interaction,
            "test": interaction,
            "user_history": pa.schema(
                [
                    pa.field("user_id", pa.string(), nullable=False),
                    pa.field("ordered_item_ids", string_list, nullable=False),
                    pa.field("ordered_timestamps", timestamp_list, nullable=False),
                    pa.field(
                        "split_cutoffs",
                        pa.struct(
                            [
                                pa.field("validation_timestamp", pa.int64()),
                                pa.field("test_timestamp", pa.int64()),
                            ]
                        ),
                        nullable=False,
                    ),
                ],
                metadata=metadata,
            ),
            "train_popularity": pa.schema(
                [
                    pa.field("item_id", pa.string(), nullable=False),
                    pa.field("count", pa.int64(), nullable=False),
                    pa.field("probability", pa.float64(), nullable=False),
                    pa.field("time_decayed_count", pa.float64(), nullable=False),
                ],
                metadata=metadata,
            ),
            "title_corpus": pa.schema(
                [
                    pa.field("item_id", pa.string(), nullable=False),
                    pa.field("normalized_title", pa.string(), nullable=False),
                    pa.field("item_split_membership", string_list, nullable=False),
                    pa.field("is_train_item", pa.bool_(), nullable=False),
                ],
                metadata=metadata,
            ),
            "event_training_signals": pa.schema(
                [
                    pa.field("event_id", pa.string(), nullable=False),
                    pa.field("user_id", pa.string(), nullable=False),
                    pa.field("item_id", pa.string(), nullable=False),
                    pa.field("server_timestamp", pa.timestamp("ms", tz="UTC"), nullable=False),
                    pa.field("signal_type", pa.string(), nullable=False),
                    pa.field("label", pa.int8()),
                    pa.field("sample_weight", pa.float64(), nullable=False),
                    pa.field("source_export_checksum", pa.string(), nullable=False),
                    pa.field("split", pa.string(), nullable=False),
                ],
                metadata=metadata,
            ),
            "events": pa.schema(
                [
                    pa.field("event_sequence_id", pa.int64(), nullable=False),
                    pa.field("event_id", pa.string(), nullable=False),
                    pa.field("user_id", pa.string(), nullable=False),
                    pa.field("request_id", pa.string(), nullable=False),
                    pa.field("item_id", pa.string(), nullable=False),
                    pa.field("position", pa.int32(), nullable=False),
                    pa.field("event_type", pa.string(), nullable=False),
                    pa.field("server_timestamp", pa.timestamp("ms", tz="UTC"), nullable=False),
                    pa.field("duration_ms", pa.int64()),
                ],
                metadata=metadata,
            ),
        }

    def _table_name(self, path: Path) -> str:
        name = path.name
        if not name.endswith(self.suffix):
            raise ValueError(f"Parquet path must end with {self.suffix}: {path}")
        return name[: -len(self.suffix)]

    def write_rows(self, path: Path, rows: Iterable[dict[str, Any]]) -> int:
        from .common import parse_utc

        pa, pq = self._arrow()
        table_name = self._table_name(path)
        schema = self.schemas().get(table_name)
        if schema is None:
            raise ValueError(f"no frozen Parquet schema for {table_name}")
        materialized = list(rows)
        if "server_timestamp" in schema.names:
            materialized = [
                {**row, "server_timestamp": parse_utc(str(row["server_timestamp"]))}
                for row in materialized
            ]
        table = pa.Table.from_pylist(materialized, schema=schema)
        pq.write_table(
            table,
            path,
            row_group_size=self.row_group_size,
            version="2.6",
            use_dictionary=False,
            compression=self.compression,
            compression_level=self.compression_level,
            write_statistics=True,
            data_page_size=1_048_576,
            data_page_version="1.0",
            use_compliant_nested_type=True,
            store_schema=True,
            write_page_index=False,
        )
        fsync_file(path)
        return len(materialized)

    def read_rows(self, path: Path) -> list[dict[str, Any]]:
        from .common import format_utc

        _, pq = self._arrow()
        table_name = self._table_name(path)
        schema = self.schemas().get(table_name)
        if schema is None:
            raise ValueError(f"no frozen Parquet schema for {table_name}")
        table = pq.read_table(path)
        if not table.schema.equals(schema, check_metadata=True):
            raise ValueError(f"Parquet schema mismatch for {table_name}")
        rows = table.to_pylist()
        if "server_timestamp" in schema.names:
            for row in rows:
                row["server_timestamp"] = format_utc(row["server_timestamp"])
        return rows
