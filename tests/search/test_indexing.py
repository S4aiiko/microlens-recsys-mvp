from __future__ import annotations

import unittest

from apps.api.app.search.domain import (
    READ_ALIAS,
    FullReindexSpec,
    IncrementalIndexSpec,
    IndexBuildConflict,
    ProjectionUnavailable,
)
from apps.api.app.search.indexing import FullReindexer, IncrementalIndexer
from tests.search._support import NOW, FakeAuthority, FakeProjection, FakeRegistry, item


class FullReindexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projection = FakeProjection()
        self.authority = FakeAuthority(
            [
                item("a", "Alpha", likes=3),
                item("b", "Beta", likes=2),
                item("z-offline", "Offline", online=False),
            ]
        )
        self.registry = FakeRegistry()
        self.reindexer = FullReindexer(
            self.projection, self.authority, self.registry, clock=lambda: NOW
        )

    def test_full_reindex_seals_then_atomically_switches_alias(self) -> None:
        result = self.reindexer.run(
            FullReindexSpec(index_version="v1", source_version="data-001", batch_size=1)
        )
        self.assertEqual(self.projection.aliases[READ_ALIAS], ("microlens-items-v1",))
        self.assertEqual(set(self.projection.indices[result.physical_index]), {"a", "b"})
        self.assertEqual(result.document_count, 2)
        self.assertEqual(len(result.projection_checksum), 64)
        self.assertFalse(result.replayed)
        self.assertEqual(self.registry.active, result.physical_index)
        self.assertEqual(self.projection.created_mappings["dynamic"], "strict")

    def test_reserved_read_alias_cannot_be_used_as_an_index_version(self) -> None:
        with self.assertRaises(ValueError):
            FullReindexSpec(index_version="read", source_version="data-001")
        with self.assertRaises(ValueError):
            FullReindexSpec(index_version="v1", source_version="data-001", batch_size=True)

    def test_projection_versions_reject_boolean_coercion(self) -> None:
        with self.assertRaises(ValueError):
            item("a", "Alpha", state_version=True)

    def test_second_version_switch_retains_old_index(self) -> None:
        self.reindexer.run(FullReindexSpec("v1", "data-001"))
        result = self.reindexer.run(
            FullReindexSpec("v2", "data-002", expected_current_index="microlens-items-v1")
        )
        self.assertEqual(result.previous_index, "microlens-items-v1")
        self.assertIn("microlens-items-v1", self.projection.indices)
        self.assertEqual(self.projection.aliases[READ_ALIAS], ("microlens-items-v2",))

    def test_bulk_or_count_failure_never_switches_old_alias(self) -> None:
        self.projection.indices["microlens-items-old"] = {}
        self.projection.aliases[READ_ALIAS] = ("microlens-items-old",)
        self.projection.bulk_fail_ids.add("b")
        with self.assertRaises(ProjectionUnavailable):
            self.reindexer.run(
                FullReindexSpec("v2", "data-002", expected_current_index="microlens-items-old")
            )
        self.assertEqual(self.projection.aliases[READ_ALIAS], ("microlens-items-old",))
        self.assertNotIn("microlens-items-v2", self.registry.builds)

    def test_existing_unsealed_index_is_dirty_and_not_reused(self) -> None:
        self.projection.indices["microlens-items-v1"] = {"a": item("a", "untrusted")}
        with self.assertRaisesRegex(IndexBuildConflict, "without an authoritative"):
            self.reindexer.run(FullReindexSpec("v1", "data-001"))

    def test_retry_after_seal_before_switch_resumes_without_rebuilding(self) -> None:
        self.projection.switch_failures = 1
        spec = FullReindexSpec("v1", "data-001")
        with self.assertRaises(ProjectionUnavailable):
            self.reindexer.run(spec)
        self.assertIn(spec.physical_index, self.registry.builds)
        replay = self.reindexer.run(spec)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.projection.aliases[READ_ALIAS], (spec.physical_index,))

    def test_retry_after_lost_switch_response_reconciles_registry(self) -> None:
        self.projection.fail_after_switch = True
        spec = FullReindexSpec("v1", "data-001")
        with self.assertRaises(ProjectionUnavailable):
            self.reindexer.run(spec)
        self.assertEqual(self.projection.aliases[READ_ALIAS], (spec.physical_index,))
        replay = self.reindexer.run(spec)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.registry.active, spec.physical_index)


class IncrementalIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projection = FakeProjection()
        self.projection.indices["microlens-items-v1"] = {
            "a": item("a", "Old Alpha"),
            "b": item("b", "Stale Beta"),
        }
        self.projection.aliases[READ_ALIAS] = ("microlens-items-v1",)
        self.authority = FakeAuthority(
            [item("a", "Current Alpha", state_version=2), item("b", "Beta", online=False)]
        )
        self.registry = FakeRegistry()
        self.indexer = IncrementalIndexer(
            self.projection, self.authority, self.registry, clock=lambda: NOW
        )

    def test_incremental_task_reloads_current_pg_state_and_is_idempotent(self) -> None:
        spec = IncrementalIndexSpec("ops-17", ("a", "b", "missing"), "operation:17", True)
        result = self.indexer.run(spec)
        documents = self.projection.indices["microlens-items-v1"]
        self.assertEqual(documents["a"].title, "Current Alpha")
        self.assertNotIn("b", documents)
        self.assertEqual((result.upserted, result.deleted), (1, 2))
        replay = self.indexer.run(spec)
        self.assertTrue(replay.replayed)
        self.assertEqual((replay.upserted, replay.deleted), (0, 0))

    def test_same_task_key_with_different_payload_is_rejected(self) -> None:
        self.indexer.run(IncrementalIndexSpec("ops-17", ("a",), "operation:17"))
        with self.assertRaisesRegex(IndexBuildConflict, "different input"):
            self.indexer.run(IncrementalIndexSpec("ops-17", ("b",), "operation:17"))

    def test_alias_change_during_bulk_requires_retry_against_new_index(self) -> None:
        self.projection.indices["microlens-items-v2"] = {}

        def switch() -> None:
            self.projection.aliases[READ_ALIAS] = ("microlens-items-v2",)
            self.projection.after_bulk = None

        self.projection.after_bulk = switch
        spec = IncrementalIndexSpec("ops-18", ("a",), "operation:18")
        with self.assertRaisesRegex(IndexBuildConflict, "alias changed"):
            self.indexer.run(spec)
        result = self.indexer.run(spec)
        self.assertEqual(result.physical_index, "microlens-items-v2")
        self.assertEqual(self.projection.indices["microlens-items-v2"]["a"].title, "Current Alpha")

    def test_partial_bulk_failure_is_not_recorded_complete(self) -> None:
        self.projection.bulk_fail_ids.add("a")
        spec = IncrementalIndexSpec("ops-19", ("a",), "operation:19")
        with self.assertRaises(ProjectionUnavailable):
            self.indexer.run(spec)
        self.assertIsNone(self.registry.incremental_fingerprint("ops-19"))


if __name__ == "__main__":
    unittest.main()
