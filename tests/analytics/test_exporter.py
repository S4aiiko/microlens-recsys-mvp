from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from recsys.analytics.contracts import (
    EVENT_TYPES,
    FEED_TYPES,
    AnalyticsContractError,
    AnalyticsSnapshot,
    EventRow,
    ExposureRow,
    TimeWindow,
)
from recsys.analytics.exporter import (
    AnalyticsExporter,
    validate_export,
    validate_export_chain,
)
from recsys.analytics.reconcile import (
    DuckDBCountReader,
    reconcile,
    reconcile_with_pyarrow,
)
from recsys.analytics.schema import (
    EVENTS_CONTRACT,
    DatasetContract,
    FieldContract,
    validate_additive_evolution,
)

NOW = datetime(2026, 8, 30, 23, 59, 30, tzinfo=UTC)


def _counts(allowed: tuple[str, ...], values: list[str]) -> dict[str, int]:
    return {kind: values.count(kind) for kind in allowed}


def fixture_snapshot() -> AnalyticsSnapshot:
    user = str(uuid.UUID(int=1))
    request_a = str(uuid.UUID(int=2))
    request_b = str(uuid.UUID(int=3))
    snapshot_a = str(uuid.UUID(int=4))
    snapshot_b = str(uuid.UUID(int=5))
    exposure_a = str(uuid.UUID(int=6))
    exposure_b = str(uuid.UUID(int=7))
    exposures = (
        ExposureRow(
            1,
            exposure_a,
            request_a,
            snapshot_a,
            user,
            "item-a",
            0,
            "popular",
            "popular",
            "model-a",
            NOW,
        ),
        ExposureRow(
            3,
            exposure_b,
            request_b,
            snapshot_b,
            user,
            "item-b",
            0,
            "personalized",
            "dssm",
            "model-a",
            NOW + timedelta(minutes=1),
        ),
    )
    events = (
        EventRow(
            1,
            str(uuid.UUID(int=11)),
            exposure_a,
            request_a,
            user,
            "item-a",
            0,
            "popular",
            "popular",
            "impression",
            None,
            NOW,
            None,
            "{}",
        ),
        EventRow(
            2,
            str(uuid.UUID(int=12)),
            exposure_a,
            request_a,
            user,
            "item-a",
            0,
            "popular",
            "popular",
            "click",
            NOW + timedelta(seconds=2),
            NOW + timedelta(seconds=2),
            None,
            '{"target":"detail"}',
        ),
        EventRow(
            3,
            str(uuid.UUID(int=13)),
            exposure_b,
            request_b,
            user,
            "item-b",
            0,
            "personalized",
            "dssm",
            "impression",
            None,
            NOW + timedelta(minutes=1),
            None,
            "{}",
        ),
    )
    return AnalyticsSnapshot(
        window=TimeWindow(NOW - timedelta(minutes=1), NOW + timedelta(minutes=2)),
        previous_event_sequence_exclusive=0,
        event_sequence_cutoff_inclusive=3,
        events=events,
        exposures=exposures,
        postgres_event_counts=_counts(EVENT_TYPES, [row.event_type for row in events]),
        postgres_exposure_counts=_counts(FEED_TYPES, [row.feed_type for row in exposures]),
    )


class AnalyticsExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exporter = AnalyticsExporter(allow_unsupported_pyarrow=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_partition_publish_validate_reconcile_and_idempotent_reuse(self) -> None:
        snapshot = fixture_snapshot()
        first = self.exporter.publish(snapshot, self.root)
        self.assertFalse(first.reused)
        self.assertTrue(
            (first.path / "events/dt=2026-08-30/event_type=click/part-00000.parquet").is_file()
        )
        self.assertTrue(
            (first.path / "events/dt=2026-08-31/event_type=impression/part-00000.parquet").is_file()
        )
        self.assertTrue(
            (
                first.path / "exposures/dt=2026-08-31/feed_type=personalized/part-00000.parquet"
            ).is_file()
        )
        manifest, checksum = validate_export(first.path, allow_unsupported_pyarrow=True)
        self.assertEqual(checksum, first.manifest_checksum)
        self.assertEqual(manifest["hive_runtime_validation"], "NOT_RUN_NO_REAL_HIVE")
        result = reconcile_with_pyarrow(first.path, allow_unsupported_version=True)
        self.assertTrue(result.matched)
        self.assertEqual(result.parquet.events["impression"], 2)
        self.assertEqual(result.parquet.exposures["popular"], 1)
        second = self.exporter.publish(snapshot, self.root)
        self.assertTrue(second.reused)
        self.assertEqual(first.manifest_checksum, second.manifest_checksum)

    def test_empty_window_is_valid_and_reconciles_to_zero(self) -> None:
        snapshot = fixture_snapshot()
        empty = replace(
            snapshot,
            events=(),
            exposures=(),
            event_sequence_cutoff_inclusive=0,
            postgres_event_counts={kind: 0 for kind in EVENT_TYPES},
            postgres_exposure_counts={kind: 0 for kind in FEED_TYPES},
        )
        result = self.exporter.publish(empty, self.root)
        self.assertTrue(reconcile_with_pyarrow(result.path, allow_unsupported_version=True).matched)

    def test_separate_builds_are_byte_identical(self) -> None:
        first = self.exporter.publish(fixture_snapshot(), self.root / "first")
        second = self.exporter.publish(fixture_snapshot(), self.root / "second")

        def bytes_by_path(path: Path) -> dict[str, bytes]:
            return {
                child.relative_to(path).as_posix(): child.read_bytes()
                for child in path.rglob("*")
                if child.is_file()
            }

        self.assertEqual(first.export_id, second.export_id)
        self.assertEqual(first.manifest_checksum, second.manifest_checksum)
        self.assertEqual(bytes_by_path(first.path), bytes_by_path(second.path))

    def test_atomic_rename_failure_leaves_no_partial_export(self) -> None:
        with patch("recsys.analytics.exporter.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, "injected"):
                self.exporter.publish(fixture_snapshot(), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_mismatched_postgres_aggregate_fails_before_writing(self) -> None:
        snapshot = fixture_snapshot()
        bad = replace(snapshot, postgres_event_counts={kind: 0 for kind in EVENT_TYPES})
        with self.assertRaisesRegex(AnalyticsContractError, "PostgreSQL aggregates"):
            self.exporter.publish(bad, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_manifest_or_parquet_tamper_fails_closed(self) -> None:
        result = self.exporter.publish(fixture_snapshot(), self.root)
        parquet = next(result.path.rglob("*.parquet"))
        parquet.write_bytes(parquet.read_bytes() + b"tamper")
        with self.assertRaisesRegex(AnalyticsContractError, "size/checksum"):
            validate_export(result.path, allow_unsupported_pyarrow=True)

    def test_follow_up_revision_advances_watermark_and_names_parent(self) -> None:
        first = self.exporter.publish(fixture_snapshot(), self.root)
        event = replace(
            fixture_snapshot().events[1],
            event_sequence_id=4,
            event_id=str(uuid.UUID(int=14)),
            event_type="like",
        )
        follow_up = AnalyticsSnapshot(
            window=fixture_snapshot().window,
            previous_event_sequence_exclusive=3,
            event_sequence_cutoff_inclusive=4,
            events=(event,),
            exposures=(),
            postgres_event_counts=_counts(EVENT_TYPES, ["like"]),
            postgres_exposure_counts={kind: 0 for kind in FEED_TYPES},
        )
        second = self.exporter.publish(
            follow_up, self.root, parent_manifest_checksum=first.manifest_checksum
        )
        self.assertNotEqual(first.export_id, second.export_id)
        manifest, _ = validate_export(second.path, allow_unsupported_pyarrow=True)
        self.assertEqual(manifest["identity"]["parent_manifest_checksum"], first.manifest_checksum)
        self.assertEqual(
            manifest["identity"]["source_watermark"]["previous_event_sequence_exclusive"],
            3,
        )
        validate_export_chain(first.path, second.path, allow_unsupported_pyarrow=True)

    def test_symlink_root_and_unlisted_parquet_are_rejected(self) -> None:
        result = self.exporter.publish(fixture_snapshot(), self.root / "real")
        linked = self.root / "linked"
        linked.symlink_to(self.root / "real", target_is_directory=True)
        with self.assertRaisesRegex(AnalyticsContractError, "symlink"):
            self.exporter.publish(fixture_snapshot(), linked)
        extra = result.path / "events/dt=2026-08-30/event_type=click/extra.parquet"
        extra.write_bytes(next(result.path.rglob("*.parquet")).read_bytes())
        with self.assertRaisesRegex(AnalyticsContractError, "unlisted"):
            validate_export(result.path, allow_unsupported_pyarrow=True)

    def test_optional_duckdb_adapter_is_dependency_injected(self) -> None:
        result = self.exporter.publish(fixture_snapshot(), self.root)

        class Cursor:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class Connection:
            def execute(self, query, parameters):
                self.assert_safe(query, parameters)
                if "event_type" in query:
                    return Cursor([("impression", 2), ("click", 1)])
                return Cursor([("personalized", 1), ("popular", 1)])

            @staticmethod
            def assert_safe(query, parameters):
                if "read_parquet(?" not in query or len(parameters) != 1:
                    raise AssertionError("path must be parameterized")

        reader = DuckDBCountReader(Connection())
        reconciled = reconcile(result.path, reader, allow_unsupported_pyarrow=True)
        self.assertTrue(reconciled.matched)
        self.assertEqual(reconciled.engine, "duckdb")

    def test_rows_reject_implicit_boolean_and_invalid_domain_values(self) -> None:
        snapshot = fixture_snapshot()
        event = snapshot.events[0]
        exposure = snapshot.exposures[0]
        invalid_rows = (
            (event, {"event_sequence_id": True}),
            (event, {"position": True}),
            (event, {"duration_ms": True}),
            (event, {"event_type": "purchase"}),
            (exposure, {"canonical_event_sequence_id": True}),
            (exposure, {"position": True}),
            (exposure, {"feed_type": "unknown"}),
        )
        for row, changes in invalid_rows:
            with self.subTest(row_type=type(row).__name__, changes=changes):
                with self.assertRaises(AnalyticsContractError):
                    replace(row, **changes)


class SchemaEvolutionTests(unittest.TestCase):
    def test_nullable_append_is_allowed(self) -> None:
        evolved = DatasetContract(
            EVENTS_CONTRACT.name,
            "1.1",
            EVENTS_CONTRACT.fields + (FieldContract("campaign_id", "string", True),),
            EVENTS_CONTRACT.partition_fields,
        )
        validate_additive_evolution(EVENTS_CONTRACT, evolved)

    def test_rename_type_required_addition_and_partition_change_fail(self) -> None:
        cases = [
            DatasetContract(
                EVENTS_CONTRACT.name,
                "1.1",
                (FieldContract("renamed", "bigint", False),) + EVENTS_CONTRACT.fields[1:],
                EVENTS_CONTRACT.partition_fields,
            ),
            DatasetContract(
                EVENTS_CONTRACT.name,
                "1.1",
                EVENTS_CONTRACT.fields + (FieldContract("campaign_id", "string", False),),
                EVENTS_CONTRACT.partition_fields,
            ),
            DatasetContract(
                EVENTS_CONTRACT.name,
                "1.1",
                EVENTS_CONTRACT.fields,
                ("event_type", "dt"),
            ),
        ]
        for contract in cases:
            with self.subTest(contract=contract):
                with self.assertRaises(AnalyticsContractError):
                    validate_additive_evolution(EVENTS_CONTRACT, contract)


class AnalyticsArtifactTests(unittest.TestCase):
    def test_frozen_config_and_hive_ddl_templates_are_explicit(self) -> None:
        import yaml

        root = Path(__file__).resolve().parents[2]
        config = yaml.safe_load((root / "configs/analytics/export-v1.yaml").read_text())
        self.assertEqual(config["schema_version"], "1.0")
        self.assertEqual(config["timezone"], "UTC")
        self.assertEqual(config["writer"]["exact_version"], "25.0.1")
        self.assertEqual(config["datasets"]["events"]["partition_fields"], ["dt", "event_type"])
        self.assertEqual(config["datasets"]["exposures"]["partition_fields"], ["dt", "feed_type"])
        for name, partition in (
            ("events", "PARTITIONED BY (dt STRING, event_type STRING)"),
            ("exposures", "PARTITIONED BY (dt STRING, feed_type STRING)"),
        ):
            ddl = (root / f"configs/analytics/hive/{name}_external_table.sql.tmpl").read_text()
            self.assertIn("NOT VALIDATED AGAINST A REAL HIVE INSTANCE", ddl)
            self.assertIn(partition, ddl)
            self.assertIn("STORED AS PARQUET", ddl)
            self.assertIn("MSCK REPAIR TABLE", ddl)
        documentation = (root / "docs/analytics-hive-compatible.md").read_text()
        self.assertIn("has **not** connected", documentation)
        self.assertIn("`duckdb==1.5.5`", documentation)


if __name__ == "__main__":
    unittest.main()
