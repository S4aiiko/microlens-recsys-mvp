from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import uuid
from collections import Counter
from pathlib import Path

from recsys.data import (
    EventExportError,
    HoldoutInsufficientError,
    JsonLinesCodec,
    build_official_dataset,
    build_training_data,
    canonical_json_bytes,
    sha256_file,
    validate_event_export,
)
from recsys.data.events import _validate_mapping

from .test_pipeline import _config, _write_raw


def _event(
    sequence: int,
    event_type: str,
    timestamp: str,
    *,
    item_id: str = "1",
    user_id: str = "online-user",
    duration_ms: int | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "event_sequence_id": sequence,
        "event_id": str(uuid.UUID(int=sequence)),
        "user_id": user_id,
        "request_id": str(uuid.UUID(int=1000 + sequence)),
        "item_id": item_id,
        "position": 0,
        "event_type": event_type,
        "server_timestamp": timestamp,
    }
    if duration_ms is not None:
        row["duration_ms"] = duration_ms
    return row


def _rejected_event(
    sequence: int,
    timestamp: str = "2026-01-01T00:00:00Z",
    *,
    item_id: str = "missing-item",
) -> dict[str, object]:
    return {
        **_event(sequence, "click", timestamp, item_id=item_id),
        "reason": "missing_item_metadata",
    }


def _write_export(
    root: Path,
    rows: list[dict[str, object]],
    cutoff: str,
    *,
    rejected_rows: list[dict[str, object]] | None = None,
    start_exclusive: int = 0,
    end_inclusive: int | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rejected_rows = rejected_rows or []
    events = root / "events.jsonl"
    events.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    rejected = root / "rejected.jsonl"
    rejected.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rejected_rows))
    all_rows = [*rows, *rejected_rows]
    reasons = Counter(str(row["reason"]) for row in rejected_rows)
    manifest = {
        "schema_version": "1.0",
        "export_id": "fixture-export",
        "event_id_ordering": "database_sequence",
        "watermark": {
            "start_exclusive": start_exclusive,
            "end_inclusive": (
                end_inclusive
                if end_inclusive is not None
                else max(
                    (int(row["event_sequence_id"]) for row in all_rows),
                    default=start_exclusive,
                )
            ),
        },
        "export_cutoff_utc": cutoff,
        "events_file": {
            "path": "events.jsonl",
            "size_bytes": events.stat().st_size,
            "sha256": sha256_file(events),
            "rows": len(rows),
        },
        "rejected_file": {
            "path": "rejected.jsonl",
            "size_bytes": rejected.stat().st_size,
            "sha256": sha256_file(rejected),
            "rows": len(rejected_rows),
        },
        "event_counts": {
            "accepted": len(rows),
            "rejected": len(rejected_rows),
            "total": len(all_rows),
        },
        "rejected_reason_counts": dict(sorted(reasons.items())),
    }
    (root / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    return root


def _mapping(*, minimum: int = 1) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "mapping_version": "fixture-v1",
        "positive_weights": {"click": 1.0, "like": 2.0, "share": 3.0, "revisit": 1.5},
        "negative_weights": {"not_interested": 1.0},
        "dwell": {"minimum_duration_ms": 1000, "maximum_duration_ms": 60000, "weight": 1.25},
        "evaluation": {
            "train_end_utc": "2026-01-02T00:00:00Z",
            "validation_start_utc": "2026-01-02T00:00:00Z",
            "validation_end_utc": "2026-01-03T00:00:00Z",
            "test_start_utc": "2026-01-03T00:00:00Z",
            "test_end_utc": "2026-01-04T00:00:00Z",
            "minimum_interactions_per_window": minimum,
            "minimum_users_per_window": minimum,
        },
    }


class EventPipelineTests(unittest.TestCase):
    def _base(self, root: Path):
        raw = root / "raw"
        _write_raw(raw)
        return build_official_dataset(_config(), raw, root / "processed", codec=JsonLinesCodec())

    def test_deterministic_quality_build_and_future_window_train_invariance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            rows = [
                _event(1, "click", "2026-01-01T00:00:00Z"),
                _event(2, "not_interested", "2026-01-01T01:00:00Z", item_id="2"),
                _event(3, "like", "2026-01-02T01:00:00Z", user_id="v-user"),
                _event(4, "share", "2026-01-03T01:00:00Z", user_id="t-user"),
            ]
            export_a = _write_export(root / "export-a", rows, "2026-01-04T00:00:00Z")
            result_a = build_training_data(
                base.data_version,
                root / "processed",
                export_a,
                _mapping(),
                "quality_evaluation",
                codec=JsonLinesCodec(),
            )
            repeat_root = root / "repeat"
            repeat_root.mkdir()
            # Reuse the immutable base under a different processed root.
            shutil.copytree(base.path, repeat_root / base.data_version)
            result_b = build_training_data(
                base.data_version,
                repeat_root,
                export_a,
                _mapping(),
                "quality_evaluation",
                codec=JsonLinesCodec(),
            )
            self.assertEqual(result_a.data_version, result_b.data_version)
            self.assertEqual(result_a.manifest_checksum, result_b.manifest_checksum)
            self.assertTrue(result_a.manifest["activation_eligible"])
            train_sha = next(
                artifact["sha256"]
                for artifact in result_a.manifest["artifacts"]
                if artifact["path"] == "train.jsonl"
            )

            changed_future = rows[:-1] + [
                _event(4, "share", "2026-01-03T01:00:00Z", item_id="2", user_id="t-user")
            ]
            export_b = _write_export(root / "export-b", changed_future, "2026-01-04T00:00:00Z")
            result_c = build_training_data(
                base.data_version,
                root / "processed",
                export_b,
                _mapping(),
                "quality_evaluation",
                codec=JsonLinesCodec(),
            )
            changed_train_sha = next(
                artifact["sha256"]
                for artifact in result_c.manifest["artifacts"]
                if artifact["path"] == "train.jsonl"
            )
            self.assertEqual(train_sha, changed_train_sha)

    def test_tamper_cutoff_duplicate_and_unknown_item_fail_closed(self) -> None:
        cases = [
            ("cutoff", [_event(1, "click", "2026-01-05T00:00:00Z")], "beyond export cutoff"),
            (
                "duplicate",
                [
                    _event(1, "click", "2026-01-01T00:00:00Z"),
                    {
                        **_event(2, "like", "2026-01-01T01:00:00Z"),
                        "event_id": str(uuid.UUID(int=1)),
                    },
                ],
                "duplicate event_id",
            ),
            (
                "unknown",
                [_event(1, "click", "2026-01-01T00:00:00Z", item_id="999")],
                "unknown item",
            ),
        ]
        for name, rows, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                base = self._base(root)
                export = _write_export(root / "export", rows, "2026-01-04T00:00:00Z")
                with self.assertRaisesRegex(EventExportError, message):
                    build_training_data(
                        base.data_version,
                        root / "processed",
                        export,
                        _mapping(),
                        "systems_only",
                        codec=JsonLinesCodec(),
                    )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            export = _write_export(
                root / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
            )
            with (export / "events.jsonl").open("ab") as handle:
                handle.write(b"{}\n")
            with self.assertRaisesRegex(EventExportError, "checksum"):
                build_training_data(
                    base.data_version,
                    root / "processed",
                    export,
                    _mapping(),
                    "systems_only",
                    codec=JsonLinesCodec(),
                )

    def test_holdout_insufficient_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            rows = [
                _event(1, "click", "2026-01-01T00:00:00Z"),
                _event(2, "like", "2026-01-02T01:00:00Z"),
                _event(3, "share", "2026-01-03T01:00:00Z"),
            ]
            export = _write_export(root / "export", rows, "2026-01-04T00:00:00Z")
            with self.assertRaisesRegex(HoldoutInsufficientError, "NOT_ENOUGH_HOLDOUT"):
                build_training_data(
                    base.data_version,
                    root / "processed",
                    export,
                    _mapping(minimum=2),
                    "quality_evaluation",
                    codec=JsonLinesCodec(),
                )

    def test_impressions_cannot_satisfy_quality_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            rows = [
                _event(1, "click", "2026-01-01T00:00:00Z"),
                _event(2, "impression", "2026-01-02T01:00:00Z", user_id="v"),
                _event(3, "impression", "2026-01-03T01:00:00Z", user_id="t"),
            ]
            export = _write_export(root / "export", rows, "2026-01-04T00:00:00Z")
            with self.assertRaisesRegex(HoldoutInsufficientError, "NOT_ENOUGH_HOLDOUT"):
                build_training_data(
                    base.data_version,
                    root / "processed",
                    export,
                    _mapping(),
                    "quality_evaluation",
                    codec=JsonLinesCodec(),
                )

    def test_export_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            export = _write_export(
                root / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
            )
            manifest_path = export / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["events_file"]["path"] = "../events.jsonl"
            manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
            with self.assertRaisesRegex(EventExportError, "relative file name"):
                build_training_data(
                    base.data_version,
                    root / "processed",
                    export,
                    _mapping(),
                    "systems_only",
                    codec=JsonLinesCodec(),
                )

    def test_export_root_manifest_and_descriptor_contract_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = _write_export(
                root / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
            )
            linked = root / "linked-export"
            linked.symlink_to(export, target_is_directory=True)
            with self.assertRaisesRegex(EventExportError, "real directory"):
                validate_event_export(linked, known_item_ids={"1"}, codec=JsonLinesCodec())

            manifest_path = export / "manifest.json"
            real_manifest = export / "manifest.real.json"
            manifest_path.rename(real_manifest)
            manifest_path.symlink_to(real_manifest.name)
            with self.assertRaisesRegex(EventExportError, "manifest is missing"):
                validate_event_export(export, known_item_ids={"1"}, codec=JsonLinesCodec())

        invalid_descriptors = [
            [],
            {"path": "events.jsonl", "size_bytes": 0, "sha256": "bad", "rows": 1},
            {
                "path": "../events.jsonl",
                "size_bytes": 0,
                "sha256": "0" * 64,
                "rows": 1,
            },
            {
                "path": "events.jsonl",
                "size_bytes": "0",
                "sha256": "0" * 64,
                "rows": 1,
            },
            {
                "path": "events.jsonl",
                "size_bytes": 0,
                "sha256": "0" * 64,
                "rows": True,
            },
        ]
        for descriptor in invalid_descriptors:
            with self.subTest(descriptor=descriptor), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                export = _write_export(
                    root / "export",
                    [_event(1, "click", "2026-01-01T00:00:00Z")],
                    "2026-01-04T00:00:00Z",
                )
                manifest_path = export / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["events_file"] = descriptor
                manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
                with self.assertRaisesRegex(EventExportError, "descriptor"):
                    validate_event_export(export, known_item_ids={"1"}, codec=JsonLinesCodec())

    def test_empty_export_watermark_and_mapping_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            valid_empty = _write_export(
                root / "valid-empty",
                [],
                "2026-01-04T00:00:00Z",
                start_exclusive=4,
                end_inclusive=4,
            )
            manifest, rows, _ = validate_event_export(
                valid_empty, known_item_ids={"1"}, codec=JsonLinesCodec()
            )
            self.assertEqual(rows, [])
            self.assertEqual(manifest["watermark"]["end_inclusive"], 4)

            invalid_empty = _write_export(
                root / "invalid-empty",
                [],
                "2026-01-04T00:00:00Z",
                start_exclusive=4,
                end_inclusive=5,
            )
            with self.assertRaisesRegex(EventExportError, "empty event export"):
                validate_event_export(invalid_empty, known_item_ids={"1"}, codec=JsonLinesCodec())

        overlap = _mapping()
        overlap["negative_weights"]["click"] = 1.0
        with self.assertRaisesRegex(EventExportError, "overlap"):
            _validate_mapping(overlap)
        dwell_override = _mapping()
        dwell_override["positive_weights"]["dwell"] = 1.0
        with self.assertRaisesRegex(EventExportError, "dwell must"):
            _validate_mapping(dwell_override)
        for field, invalid in (
            ("minimum_duration_ms", True),
            ("maximum_duration_ms", False),
            ("weight", float("nan")),
            ("weight", float("inf")),
        ):
            with self.subTest(field=field, invalid=invalid):
                bad_dwell = _mapping()
                bad_dwell["dwell"][field] = invalid
                with self.assertRaisesRegex(EventExportError, "dwell mapping"):
                    _validate_mapping(bad_dwell)

    def test_rejected_rows_cover_middle_tail_and_all_rejected_ranges(self) -> None:
        fixtures = (
            (
                "middle",
                [
                    _event(1, "click", "2026-01-01T00:00:00Z"),
                    _event(3, "like", "2026-01-01T02:00:00Z"),
                ],
                [_rejected_event(2, "2026-01-01T01:00:00Z")],
                [1, 3],
            ),
            (
                "tail",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [
                    _rejected_event(2, "2026-01-01T01:00:00Z"),
                    _rejected_event(3, "2026-01-01T02:00:00Z"),
                ],
                [1],
            ),
            (
                "all",
                [],
                [
                    _rejected_event(1, "2026-01-01T00:00:00Z"),
                    _rejected_event(2, "2026-01-01T01:00:00Z"),
                ],
                [],
            ),
            (
                "database-sequence-allocation-gap",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(3, "2026-01-01T02:00:00Z")],
                [1],
            ),
        )
        for name, accepted, rejected, expected_sequences in fixtures:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                export = _write_export(
                    Path(temp) / "export",
                    accepted,
                    "2026-01-04T00:00:00Z",
                    rejected_rows=rejected,
                )
                manifest, rows, _checksum = validate_event_export(
                    export, known_item_ids={"1", "2"}, codec=JsonLinesCodec()
                )
                self.assertEqual([row["event_sequence_id"] for row in rows], expected_sequences)
                self.assertEqual(manifest["event_counts"]["rejected"], len(rejected))

    def test_rejected_export_integrity_failures_are_rejected(self) -> None:
        mutations = (
            (
                "missing descriptor",
                lambda export, manifest: manifest.pop("rejected_file"),
                "missing fields",
            ),
            (
                "descriptor traversal",
                lambda export, manifest: manifest["rejected_file"].__setitem__(
                    "path", "../rejected.jsonl"
                ),
                "descriptor",
            ),
            (
                "descriptor rows",
                lambda export, manifest: manifest["rejected_file"].__setitem__("rows", 2),
                "row count mismatch",
            ),
            (
                "reason counts",
                lambda export, manifest: manifest.__setitem__(
                    "rejected_reason_counts", {"missing_item_metadata": 2}
                ),
                "reason counts mismatch",
            ),
            (
                "event counts bool",
                lambda export, manifest: manifest["event_counts"].__setitem__("rejected", True),
                "counts mismatch",
            ),
        )
        for name, mutate, message in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                export = _write_export(
                    Path(temp) / "export",
                    [_event(1, "click", "2026-01-01T00:00:00Z")],
                    "2026-01-04T00:00:00Z",
                    rejected_rows=[_rejected_event(2)],
                )
                manifest_path = export / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                mutate(export, manifest)
                manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
                with self.assertRaisesRegex(EventExportError, message):
                    validate_event_export(export, known_item_ids={"1", "2"}, codec=JsonLinesCodec())

        with tempfile.TemporaryDirectory() as temp:
            export = _write_export(
                Path(temp) / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
                rejected_rows=[_rejected_event(2)],
            )
            with (export / "rejected.jsonl").open("ab") as handle:
                handle.write(b"{}\n")
            with self.assertRaisesRegex(EventExportError, "size/checksum"):
                validate_event_export(export, known_item_ids={"1", "2"}, codec=JsonLinesCodec())

        with tempfile.TemporaryDirectory() as temp:
            export = _write_export(
                Path(temp) / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
                rejected_rows=[_rejected_event(2)],
            )
            rejected_path = export / "rejected.jsonl"
            real_path = export / "rejected.real.jsonl"
            rejected_path.rename(real_path)
            rejected_path.symlink_to(real_path.name)
            with self.assertRaisesRegex(EventExportError, "data file is missing"):
                validate_event_export(export, known_item_ids={"1", "2"}, codec=JsonLinesCodec())

    def test_manifest_replacement_while_codec_reads_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            export = _write_export(
                Path(temp) / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
                rejected_rows=[_rejected_event(2)],
            )
            manifest_path = export / "manifest.json"

            class ManifestReplacingCodec(JsonLinesCodec):
                replaced = False

                def read_rows(self, path: Path) -> list[dict[str, object]]:
                    rows = super().read_rows(path)
                    if not self.replaced:
                        manifest = json.loads(manifest_path.read_text())
                        manifest["export_id"] = "replacement-export"
                        replacement = export / "manifest.replacement.json"
                        replacement.write_bytes(canonical_json_bytes(manifest) + b"\n")
                        replacement.replace(manifest_path)
                        self.replaced = True
                    return rows

            with self.assertRaisesRegex(EventExportError, "manifest changed"):
                validate_event_export(
                    export,
                    known_item_ids={"1", "2"},
                    codec=ManifestReplacingCodec(),
                )

    def test_rejected_partition_overlap_duplicate_tail_and_semantics_fail_closed(self) -> None:
        cases = (
            (
                "sequence overlap",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(1)],
                None,
                "sequence overlap",
            ),
            (
                "cross event id",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [{**_rejected_event(2), "event_id": str(uuid.UUID(int=1))}],
                None,
                "duplicate event_id",
            ),
            (
                "missing watermark tail",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(2)],
                3,
                "does not cover watermark end",
            ),
            (
                "known rejected item",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [_rejected_event(2, item_id="2")],
                None,
                "unknown/incomplete",
            ),
            (
                "unstable reason",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                [{**_rejected_event(2), "reason": "other"}],
                None,
                "unstable reason",
            ),
        )
        for name, accepted, rejected, end, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                export = _write_export(
                    Path(temp) / "export",
                    accepted,
                    "2026-01-04T00:00:00Z",
                    rejected_rows=rejected,
                    end_inclusive=end,
                )
                with self.assertRaisesRegex(EventExportError, message):
                    validate_event_export(export, known_item_ids={"1", "2"}, codec=JsonLinesCodec())


if __name__ == "__main__":
    unittest.main()
