from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api.app.search.domain import IndexBuildConflict, IndexBuildManifest
from apps.api.app.search.runtime import (
    SEARCH_RUNTIME_METADATA,
    PostgresSerializedRunner,
    SqlAlchemySearchIndexRegistry,
)


def manifest(name: str, fingerprint: str, *, built_at: datetime) -> IndexBuildManifest:
    return IndexBuildManifest(
        physical_index=name,
        source_version=f"data-{name}",
        build_fingerprint=fingerprint,
        document_count=3,
        projection_checksum="c" * 64,
        built_at=built_at,
    )


class Recorder:
    def __init__(self) -> None:
        self.values: list[object] = []

    def run(self, spec: object) -> object:
        self.values.append(spec)
        return spec


class SearchRuntimeRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        SEARCH_RUNTIME_METADATA.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.registry = SqlAlchemySearchIndexRegistry(self.sessions)
        self.now = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_build_activation_switch_and_incremental_receipt_are_durable(self) -> None:
        first = manifest("microlens-items-v1", "a" * 64, built_at=self.now)
        second = manifest(
            "microlens-items-v2",
            "b" * 64,
            built_at=self.now + timedelta(seconds=1),
        )
        self.registry.record_build(first)
        self.registry.record_build(first)
        self.registry.mark_active(
            first.physical_index,
            previous_index=None,
            activated_at=self.now,
        )
        self.registry.record_incremental(
            "incremental-1",
            fingerprint="d" * 64,
            physical_index=first.physical_index,
            source_watermark="items:7",
            completed_at=self.now,
        )
        self.assertEqual(self.registry.incremental_fingerprint("incremental-1"), "d" * 64)
        self.assertEqual(self.registry.last_source_watermark(), "items:7")

        self.registry.record_build(second)
        self.registry.mark_active(
            second.physical_index,
            previous_index=first.physical_index,
            activated_at=self.now + timedelta(seconds=2),
        )
        self.assertEqual(self.registry.get_build(second.physical_index), second)

    def test_registry_rejects_immutable_identity_reuse(self) -> None:
        original = manifest("microlens-items-v1", "a" * 64, built_at=self.now)
        self.registry.record_build(original)
        with self.assertRaises(IndexBuildConflict):
            self.registry.record_build(manifest("microlens-items-v1", "b" * 64, built_at=self.now))
        with self.assertRaises(IndexBuildConflict):
            self.registry.record_build(manifest("microlens-items-v2", "a" * 64, built_at=self.now))

    def test_replayed_activation_is_a_database_no_op(self) -> None:
        build = manifest("microlens-items-v1", "a" * 64, built_at=self.now)
        self.registry.record_build(build)
        self.registry.mark_active(
            build.physical_index,
            previous_index=None,
            activated_at=self.now,
        )
        with self.engine.connect() as connection:
            before = connection.execute(
                text(
                    "SELECT b.activated_at, b.previous_index, r.generation, r.updated_at "
                    "FROM search_index_builds b JOIN search_index_registry r "
                    "ON r.active_physical_index = b.physical_index"
                )
            ).one()

        self.registry.mark_active(
            build.physical_index,
            previous_index=None,
            activated_at=self.now + timedelta(minutes=5),
        )

        with self.engine.connect() as connection:
            after = connection.execute(
                text(
                    "SELECT b.activated_at, b.previous_index, r.generation, r.updated_at "
                    "FROM search_index_builds b JOIN search_index_registry r "
                    "ON r.active_physical_index = b.physical_index"
                )
            ).one()
        self.assertEqual(after, before)

    def test_non_postgres_test_runner_uses_bounded_process_lock(self) -> None:
        recorder = Recorder()
        runner = PostgresSerializedRunner(self.engine, recorder)
        self.assertEqual(runner.run("spec"), "spec")
        self.assertEqual(recorder.values, ["spec"])


if __name__ == "__main__":
    unittest.main()
