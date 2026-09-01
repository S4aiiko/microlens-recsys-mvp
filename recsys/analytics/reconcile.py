from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import EVENT_TYPES, FEED_TYPES, AnalyticsContractError
from .exporter import validate_export
from .schema import pyarrow_schemas


@dataclass(frozen=True, slots=True)
class DatasetCounts:
    events: dict[str, int]
    exposures: dict[str, int]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    engine: str
    matched: bool
    postgres: DatasetCounts
    parquet: DatasetCounts
    manifest_checksum: str


class ParquetCountReader(Protocol):
    name: str

    def counts(self, export_path: Path) -> DatasetCounts: ...


class PyArrowCountReader:
    name = "pyarrow"

    def __init__(self, *, allow_unsupported_version: bool = False) -> None:
        self.allow_unsupported_version = allow_unsupported_version

    def counts(self, export_path: Path) -> DatasetCounts:
        pa, _ = pyarrow_schemas(allow_unsupported_version=self.allow_unsupported_version)
        import pyarrow.dataset as ds

        def grouped(dataset: str, partition: str, allowed: tuple[str, ...]) -> dict[str, int]:
            root = export_path / dataset
            if not root.exists():
                return {kind: 0 for kind in allowed}
            arrow_dataset = ds.dataset(
                root,
                format="parquet",
                partitioning=ds.partitioning(flavor="hive"),
            )
            table = arrow_dataset.to_table(columns=[partition])
            counter = Counter(str(value.as_py()) for value in table[partition])
            unknown = set(counter) - set(allowed)
            if unknown:
                raise AnalyticsContractError(f"unknown {dataset} partitions: {sorted(unknown)}")
            return {kind: counter[kind] for kind in allowed}

        result = DatasetCounts(
            events=grouped("events", "event_type", EVENT_TYPES),
            exposures=grouped("exposures", "feed_type", FEED_TYPES),
        )
        del pa
        return result


class DuckDBCountReader:
    """Optional adapter; dependency ownership remains with Project Integration."""

    name = "duckdb"

    def __init__(self, connection=None) -> None:
        if connection is None:
            try:
                import duckdb
            except ImportError as exc:
                raise RuntimeError(
                    "DuckDB runtime is not pinned; request the Phase 2G dependency integration gate"
                ) from exc
            connection = duckdb.connect(":memory:")
        self.connection = connection

    def counts(self, export_path: Path) -> DatasetCounts:
        def grouped(dataset: str, key: str, allowed: tuple[str, ...]) -> dict[str, int]:
            files = sorted((export_path / dataset).glob("dt=*/**/*.parquet"))
            if not files:
                return {kind: 0 for kind in allowed}
            glob_path = (export_path / dataset / "**" / "*.parquet").as_posix()
            rows = self.connection.execute(
                f"SELECT {key}, count(*) FROM read_parquet(?, hive_partitioning=true) "
                f"GROUP BY {key}",
                [glob_path],
            ).fetchall()
            counter = Counter({str(value): int(count) for value, count in rows})
            unknown = set(counter) - set(allowed)
            if unknown:
                raise AnalyticsContractError(f"unknown {dataset} partitions: {sorted(unknown)}")
            return {kind: counter[kind] for kind in allowed}

        return DatasetCounts(
            events=grouped("events", "event_type", EVENT_TYPES),
            exposures=grouped("exposures", "feed_type", FEED_TYPES),
        )


def reconcile(
    export_path: str | Path,
    reader: ParquetCountReader,
    *,
    allow_unsupported_pyarrow: bool = False,
) -> ReconciliationResult:
    path = Path(export_path)
    manifest, checksum = validate_export(path, allow_unsupported_pyarrow=allow_unsupported_pyarrow)
    declared = manifest["postgres_counts"]
    postgres = DatasetCounts(
        events={kind: int(declared["events"][kind]) for kind in EVENT_TYPES},
        exposures={kind: int(declared["exposures"][kind]) for kind in FEED_TYPES},
    )
    parquet = reader.counts(path)
    return ReconciliationResult(
        engine=reader.name,
        matched=postgres == parquet,
        postgres=postgres,
        parquet=parquet,
        manifest_checksum=checksum,
    )


def reconcile_with_pyarrow(
    export_path: str | Path, *, allow_unsupported_version: bool = False
) -> ReconciliationResult:
    return reconcile(
        export_path,
        PyArrowCountReader(allow_unsupported_version=allow_unsupported_version),
        allow_unsupported_pyarrow=allow_unsupported_version,
    )
