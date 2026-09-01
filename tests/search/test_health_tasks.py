from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace

from apps.api.app.search.domain import (
    READ_ALIAS,
    FullReindexResult,
    IncrementalIndexResult,
    IndexHealth,
)
from apps.api.app.search.health import SearchHealthService
from apps.worker.search_tasks import FullReindexTaskHandler, IncrementalIndexTaskHandler
from tests.search._support import FakeAuthority, FakeProjection, FakeRegistry


class HealthTests(unittest.TestCase):
    def test_health_is_healthy_only_with_single_registered_alias_and_pg(self) -> None:
        projection = FakeProjection()
        authority = FakeAuthority([])
        registry = FakeRegistry()
        projection.indices["microlens-items-v1"] = {}
        projection.aliases[READ_ALIAS] = ("microlens-items-v1",)
        from apps.api.app.search.domain import IndexBuildManifest
        from tests.search._support import NOW

        registry.builds["microlens-items-v1"] = IndexBuildManifest(
            physical_index="microlens-items-v1",
            source_version="data-1",
            build_fingerprint="f" * 64,
            document_count=0,
            projection_checksum="e" * 64,
            built_at=NOW,
        )
        report = SearchHealthService(projection, authority, registry).report()
        self.assertEqual(report.status, IndexHealth.HEALTHY)
        projection.reachable = False
        degraded = SearchHealthService(projection, authority, registry).report()
        self.assertEqual(degraded.status, IndexHealth.DEGRADED)
        authority.fail = True
        unavailable = SearchHealthService(projection, authority, registry).report()
        self.assertEqual(unavailable.status, IndexHealth.UNAVAILABLE)

    def test_registry_failure_degrades_health_instead_of_raising(self) -> None:
        projection = FakeProjection()
        authority = FakeAuthority([])
        projection.indices["microlens-items-v1"] = {}
        projection.aliases[READ_ALIAS] = ("microlens-items-v1",)

        class FailedRegistry(FakeRegistry):
            def get_build(self, physical_index):
                del physical_index
                raise ConnectionError("postgres registry unavailable")

            def last_source_watermark(self):
                raise ConnectionError("postgres registry unavailable")

        report = SearchHealthService(projection, authority, FailedRegistry()).report()
        self.assertEqual(report.status, IndexHealth.DEGRADED)
        self.assertIn("index_registry_unavailable", report.reasons)


class _FullRunner:
    def __init__(self) -> None:
        self.spec = None

    def run(self, spec):
        self.spec = spec
        return FullReindexResult(
            physical_index=spec.physical_index,
            previous_index=spec.expected_current_index,
            document_count=17,
            projection_checksum="a" * 64,
            replayed=False,
        )


class _IncrementalRunner:
    def __init__(self) -> None:
        self.spec = None

    def run(self, spec):
        self.spec = spec
        return IncrementalIndexResult(
            physical_index="microlens-items-v1",
            upserted=1,
            deleted=1,
            source_watermark=spec.source_watermark,
            replayed=False,
        )


class TaskHandlerTests(unittest.TestCase):
    def claim(self, payload):
        return SimpleNamespace(job=SimpleNamespace(payload=payload, job_id=uuid.uuid4()))

    def test_full_reindex_handler_has_strict_json_contract(self) -> None:
        runner = _FullRunner()
        handler = FullReindexTaskHandler(runner)
        result = handler.handle(
            self.claim(
                {
                    "index_version": "v1",
                    "source_version": "data-1",
                    "batch_size": 500,
                    "expected_current_index": None,
                }
            ),
            now=None,
        )
        self.assertEqual(result["document_count"], 17)
        self.assertEqual(runner.spec.source_version, "data-1")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            handler.handle(
                self.claim(
                    {
                        "index_version": "v1",
                        "source_version": "data-1",
                        "batch_size": 500,
                        "shell": "do not execute",
                    }
                ),
                now=None,
            )

    def test_incremental_handler_validates_array_and_boolean_types(self) -> None:
        runner = _IncrementalRunner()
        handler = IncrementalIndexTaskHandler(runner)
        result = handler.handle(
            self.claim(
                {
                    "task_key": "ops-1",
                    "item_ids": ["a", "b"],
                    "source_watermark": "operation:1",
                    "refresh": False,
                }
            ),
            now=None,
        )
        self.assertEqual(result["deleted"], 1)
        with self.assertRaisesRegex(ValueError, "refresh"):
            handler.handle(
                self.claim(
                    {
                        "task_key": "ops-2",
                        "item_ids": ["a"],
                        "source_watermark": "operation:2",
                        "refresh": 1,
                    }
                ),
                now=None,
            )


if __name__ == "__main__":
    unittest.main()
