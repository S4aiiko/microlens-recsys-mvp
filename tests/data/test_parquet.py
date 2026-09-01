from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
import uuid
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from test_events import _event, _mapping, _rejected_event
from test_pipeline import _config, _write_raw

from recsys.data import (
    EventExportError,
    ParquetCodec,
    build_official_dataset,
    build_training_data,
    canonical_json_bytes,
    sha256_file,
    validate_event_export,
)
from recsys.data.common import fsync_file, parse_utc


def _write_rejected_parquet(codec: ParquetCodec, path: Path, rows: list[dict[str, object]]) -> None:
    pa, pq = codec._arrow()
    schema = pa.schema(
        [
            *codec.schemas()["events"],
            pa.field("reason", pa.string(), nullable=False),
        ],
        metadata={b"recsys_table_contract": b"phase-2d-event-rejections-v1"},
    )
    materialized = [
        {**row, "server_timestamp": parse_utc(str(row["server_timestamp"]))} for row in rows
    ]
    table = pa.Table.from_pylist(materialized, schema=schema)
    pq.write_table(
        table,
        path,
        row_group_size=codec.row_group_size,
        version="2.6",
        use_dictionary=False,
        compression=codec.compression,
        compression_level=codec.compression_level,
        write_statistics=True,
        data_page_size=1_048_576,
        data_page_version="1.0",
        use_compliant_nested_type=True,
        store_schema=True,
        write_page_index=False,
    )
    fsync_file(path)


def _write_parquet_export(
    root: Path,
    codec: ParquetCodec,
    accepted: list[dict[str, object]],
    rejected: list[dict[str, object]] | None = None,
    *,
    end_inclusive: int | None = None,
) -> Path:
    root.mkdir()
    rejected = rejected or []
    events_path = root / "events.parquet"
    rejected_path = root / "rejected.parquet"
    codec.write_rows(events_path, accepted)
    _write_rejected_parquet(codec, rejected_path, rejected)
    all_rows = [*accepted, *rejected]
    reasons = Counter(str(row["reason"]) for row in rejected)
    manifest = {
        "schema_version": "1.0",
        "export_id": "parquet-fixture",
        "event_id_ordering": "database_sequence",
        "watermark": {
            "start_exclusive": 0,
            "end_inclusive": (
                end_inclusive
                if end_inclusive is not None
                else max((int(row["event_sequence_id"]) for row in all_rows), default=0)
            ),
        },
        "export_cutoff_utc": "2026-01-04T00:00:00Z",
        "events_file": {
            "path": "events.parquet",
            "size_bytes": events_path.stat().st_size,
            "sha256": sha256_file(events_path),
            "rows": len(accepted),
        },
        "rejected_file": {
            "path": "rejected.parquet",
            "size_bytes": rejected_path.stat().st_size,
            "sha256": sha256_file(rejected_path),
            "rows": len(rejected),
        },
        "event_counts": {
            "accepted": len(accepted),
            "rejected": len(rejected),
            "total": len(all_rows),
        },
        "rejected_reason_counts": dict(sorted(reasons.items())),
    }
    (root / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    return root


@unittest.skipUnless(
    importlib.util.find_spec("pyarrow"), "approved pyarrow data dependency unavailable"
)
class ParquetCodecTests(unittest.TestCase):
    @staticmethod
    def _codec() -> ParquetCodec:
        # The repository CLI is fail-closed on exact 25.0.1. This test escape hatch
        # permits local fixture verification on the pre-existing host Arrow only;
        # the linux/arm64 locked run exercises the exact version.
        return ParquetCodec(allow_unsupported_version=True)

    def test_real_artifacts_round_trip_and_byte_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            _write_raw(raw)
            codec = self._codec()
            first = build_official_dataset(_config(), raw, root / "first", codec=codec)
            second = build_official_dataset(_config(), raw, root / "second", codec=codec)
            self.assertEqual(first.manifest, second.manifest)
            for artifact in first.manifest["artifacts"]:
                if not artifact["path"].endswith(".parquet"):
                    continue
                first_path = first.path / artifact["path"]
                second_path = second.path / artifact["path"]
                self.assertEqual(sha256_file(first_path), sha256_file(second_path))
                rows = codec.read_rows(first_path)
                self.assertEqual(len(rows), artifact["rows"])
                self.assertEqual(codec.write_rows(root / artifact["path"], rows), len(rows))
                self.assertEqual(sha256_file(first_path), sha256_file(root / artifact["path"]))

    def test_writer_contract_is_frozen(self) -> None:
        codec = self._codec()
        self.assertEqual(codec.row_group_size, 65_536)
        self.assertEqual(codec.compression, "zstd")
        self.assertEqual(codec.compression_level, 3)
        self.assertEqual(
            codec.schemas()["train"].names,
            ["user_id", "item_id", "timestamp"],
        )
        self.assertEqual(
            codec.schemas()["event_training_signals"].field("server_timestamp").type.unit,
            "ms",
        )

    def test_public_codec_enforces_exact_locked_version(self) -> None:
        codec = ParquetCodec()
        if version("pyarrow") == codec.expected_pyarrow_version:
            self.assertIn("train", codec.schemas())
        else:
            with self.assertRaisesRegex(RuntimeError, "requires exact pyarrow"):
                codec.schemas()

    def test_public_builders_default_to_fail_closed_parquet(self) -> None:
        strict_codec = ParquetCodec()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            _write_raw(raw)
            if version("pyarrow") != strict_codec.expected_pyarrow_version:
                with self.assertRaisesRegex(RuntimeError, "requires exact pyarrow"):
                    build_official_dataset(_config(), raw, root / "processed")
                with self.assertRaisesRegex(RuntimeError, "requires exact pyarrow"):
                    build_training_data(
                        "missing-version",
                        root / "processed",
                        root / "export",
                        _mapping(),
                        "systems_only",
                    )
                return

            base = build_official_dataset(_config(), raw, root / "processed")
            self.assertEqual(
                base.manifest["output_schema"]["storage_format"],
                strict_codec.base_format_name,
            )
            self.assertTrue((base.path / "train.parquet").is_file())
            export = _write_parquet_export(
                root / "export",
                strict_codec,
                [_event(1, "click", "2026-01-01T00:00:00Z")],
            )
            derived = build_training_data(
                base.data_version,
                root / "processed",
                export,
                _mapping(),
                "systems_only",
            )
            self.assertTrue((derived.path / "event_training_signals.parquet").is_file())

    def test_event_export_and_derived_artifacts_are_deterministic_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            _write_raw(raw)
            codec = self._codec()
            base = build_official_dataset(_config(), raw, root / "processed-a", codec=codec)
            rows = [
                _event(1, "click", "2026-01-01T00:00:00Z"),
                _event(2, "not_interested", "2026-01-01T01:00:00Z", item_id="2"),
            ]
            export = _write_parquet_export(root / "export", codec, rows)
            first = build_training_data(
                base.data_version,
                root / "processed-a",
                export,
                _mapping(),
                "systems_only",
                codec=codec,
            )
            second_root = root / "processed-b"
            second_root.mkdir()
            shutil.copytree(base.path, second_root / base.data_version)
            second = build_training_data(
                base.data_version,
                second_root,
                export,
                _mapping(),
                "systems_only",
                codec=codec,
            )
            self.assertEqual(first.manifest, second.manifest)
            self.assertEqual(first.manifest_checksum, second.manifest_checksum)
            for artifact in first.manifest["artifacts"]:
                if artifact["path"].endswith(".parquet"):
                    self.assertEqual(
                        sha256_file(first.path / artifact["path"]),
                        sha256_file(second.path / artifact["path"]),
                    )

    def test_rejected_parquet_middle_tail_and_all_rejected_are_valid(self) -> None:
        codec = self._codec()
        cases = (
            (
                [
                    _event(1, "click", "2026-01-01T00:00:00Z"),
                    _event(3, "like", "2026-01-01T02:00:00Z"),
                ],
                [_rejected_event(2, "2026-01-01T01:00:00Z")],
                [1, 3],
            ),
            (
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(2), _rejected_event(3)],
                [1],
            ),
            ([], [_rejected_event(1), _rejected_event(2)], []),
        )
        for index, (accepted, rejected, expected) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                export = _write_parquet_export(Path(temp) / "export", codec, accepted, rejected)
                _manifest, rows, _checksum = validate_event_export(
                    export, known_item_ids={"1", "2"}, codec=codec
                )
                self.assertEqual([row["event_sequence_id"] for row in rows], expected)

    def test_rejected_parquet_tamper_and_cross_file_duplicates_fail_closed(self) -> None:
        codec = self._codec()
        with tempfile.TemporaryDirectory() as temp:
            export = _write_parquet_export(
                Path(temp) / "export",
                codec,
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(2)],
            )
            with (export / "rejected.parquet").open("ab") as handle:
                handle.write(b"tamper")
            with self.assertRaisesRegex(EventExportError, "size/checksum"):
                validate_event_export(export, known_item_ids={"1", "2"}, codec=codec)

        for name, rejected, message in (
            ("sequence", [_rejected_event(1)], "sequence overlap"),
            (
                "event-id",
                [{**_rejected_event(2), "event_id": str(uuid.UUID(int=1))}],
                "duplicate event_id",
            ),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                export = _write_parquet_export(
                    Path(temp) / "export",
                    codec,
                    [_event(1, "click", "2026-01-01T00:00:00Z")],
                    rejected,
                )
                with self.assertRaisesRegex(EventExportError, message):
                    validate_event_export(export, known_item_ids={"1", "2"}, codec=codec)

        with tempfile.TemporaryDirectory() as temp:
            export = _write_parquet_export(
                Path(temp) / "export",
                codec,
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(3)],
                end_inclusive=3,
            )
            _manifest, rows, _checksum = validate_event_export(
                export, known_item_ids={"1", "2"}, codec=codec
            )
            self.assertEqual([row["event_sequence_id"] for row in rows], [1])

        with tempfile.TemporaryDirectory() as temp:
            export = _write_parquet_export(
                Path(temp) / "export",
                codec,
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(2)],
                end_inclusive=3,
            )
            with self.assertRaisesRegex(EventExportError, "does not cover watermark end"):
                validate_event_export(export, known_item_ids={"1", "2"}, codec=codec)


if __name__ == "__main__":
    unittest.main()
