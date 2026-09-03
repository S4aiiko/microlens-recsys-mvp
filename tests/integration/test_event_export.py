from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.cli.export_training_events import EXIT_CONFIGURATION, run
from apps.api.app.db import Base
from apps.api.app.db.models import (
    AccountStatus,
    Event,
    EventType,
    Exposure,
    FeedType,
    Item,
    RecommendationRequest,
    RecommendationSnapshot,
    Role,
    TrainingExportWatermark,
    User,
)
from apps.api.app.events.export import (
    EventExportError,
    ExportNamespaceCollision,
    ExportRange,
    ExportWatermarkCASFailure,
    TrainingEventExporter,
    TrainingExportRepository,
)
from recsys.data import ParquetCodec, validate_event_export
from recsys.data.common import canonical_json_bytes, sha256_file

DATABASE_ENV = "EVENT_EXPORT_TEST_DATABASE_URL"
NOW = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)


def test_cli_configuration_exit_is_stable_and_does_not_echo_secrets(capsys) -> None:
    secret = "definitely-not-for-output"
    code = run([], environ={"DATABASE_URL": f"postgresql://user:{secret}@db/example"})
    captured = capsys.readouterr()
    assert code == EXIT_CONFIGURATION
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.parametrize(
    "name",
    ["../escape", ".hidden", "contains space", "名字", "a" * 256],
)
def test_cli_rejects_unsafe_watermark_name_before_database_access(name: str, capsys) -> None:
    code = run(
        ["--watermark-name", name],
        environ={
            "DATABASE_URL": "postgresql://must-not-be-contacted",
            "TRAINING_EXPORTS_DIR": "/must-not-be-created",
        },
    )
    captured = capsys.readouterr()
    assert code == EXIT_CONFIGURATION
    assert captured.out == ""
    assert captured.err == "export-training-events: invalid watermark name\n"


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    database_url = os.environ.get(DATABASE_ENV)
    if not database_url:
        pytest.skip(f"set {DATABASE_ENV} to an isolated PostgreSQL 16 database")
    import pyarrow

    assert pyarrow.__version__ == "25.0.1"
    admin = create_engine(database_url, pool_pre_ping=True)
    schema = f"event_export_{uuid.uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture()
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    with postgres_engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    return sessionmaker(bind=postgres_engine, expire_on_commit=False, autoflush=False)


def _seed_event_graph(
    factory: sessionmaker[Session],
    *,
    complete_events: int = 2,
    official_snapshot_events: int = 0,
    incomplete_events: int = 0,
    gap_after_event: int | None = None,
) -> list[int]:
    sequence_ids: list[int] = []
    with factory.begin() as session:
        user = User(
            id=uuid.uuid4(),
            username="export_user",
            username_normalized="export_user",
            password_hash="not-a-real-login-hash",
            role=Role.USER,
            status=AccountStatus.ENABLED,
            created_at=NOW,
        )
        complete = Item(
            id="complete-item",
            title="Complete metadata",
            likes_snapshot=1,
            views_snapshot=2,
            metadata_status="complete",
            updated_at=NOW,
        )
        incomplete = Item(
            id="incomplete-item",
            title="Incomplete metadata",
            likes_snapshot=None,
            views_snapshot=None,
            metadata_status="missing",
            updated_at=NOW,
        )
        official_snapshot = Item(
            id="official-snapshot-item",
            title="Official metadata with point-in-time feature restriction",
            likes_snapshot=3,
            views_snapshot=4,
            metadata_status="complete_snapshot_unusable_as_of_feature",
            updated_at=NOW,
        )
        session.add_all([user, complete, official_snapshot, incomplete])
        session.flush()
        snapshot = RecommendationSnapshot(
            snapshot_id=uuid.uuid4(),
            user_id=user.id,
            feed_type=FeedType.PERSONALIZED,
            model_version="fixture-model",
            snapshot_seed=7,
            expires_at=NOW + timedelta(hours=1),
            created_at=NOW,
        )
        session.add(snapshot)
        session.flush()
        request = RecommendationRequest(
            request_id=uuid.uuid4(),
            snapshot_id=snapshot.snapshot_id,
            user_id=user.id,
            offset=0,
            limit=100,
            latency_ms=1,
            created_at=NOW,
        )
        session.add(request)
        session.flush()
        event_number = 0
        for item, count in (
            (complete, complete_events),
            (official_snapshot, official_snapshot_events),
            (incomplete, incomplete_events),
        ):
            for _ in range(count):
                exposure = Exposure(
                    id=uuid.uuid4(),
                    request_id=request.request_id,
                    snapshot_id=snapshot.snapshot_id,
                    user_id=user.id,
                    item_id=item.id,
                    position=event_number,
                    source="fixture",
                    model_version="fixture-model",
                    exposed_at=NOW + timedelta(seconds=event_number),
                )
                row = Event(
                    event_id=uuid.uuid4(),
                    exposure_id=exposure.id,
                    request_id=request.request_id,
                    user_id=user.id,
                    item_id=item.id,
                    position=event_number,
                    feed_type=FeedType.PERSONALIZED,
                    source="fixture",
                    event_type=EventType.CLICK,
                    # Deliberately reverse client chronology. Inclusion/order must
                    # remain controlled exclusively by the DB sequence id.
                    client_timestamp=NOW - timedelta(days=event_number + 1),
                    server_timestamp=NOW + timedelta(seconds=event_number),
                    duration_ms=None,
                    payload={},
                    payload_hash=f"{event_number:064x}",
                )
                session.add(exposure)
                session.flush()
                session.add(row)
                session.flush()
                sequence_ids.append(row.id)
                event_number += 1
                if event_number == gap_after_event:
                    session.scalar(text("SELECT nextval(pg_get_serial_sequence('events', 'id'))"))
    return sequence_ids


def _watermark(factory: sessionmaker[Session], name: str = "online-events"):
    with factory() as session:
        return session.get(TrainingExportWatermark, name)


def test_empty_export_is_real_parquet_canonical_and_validator_accepted(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory.begin() as session:
        result = TrainingEventExporter().export(session, output_root=tmp_path)
    assert (result.start_exclusive, result.end_inclusive) == (0, 0)
    assert (result.accepted, result.rejected) == (0, 0)
    manifest_bytes = (result.path / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest_bytes == canonical_json_bytes(manifest)
    assert manifest["schema_version"] == "1.0"
    assert manifest["event_id_ordering"] == "database_sequence"
    assert manifest["events_file"]["rows"] == 0
    assert manifest["rejected_file"]["rows"] == 0
    validate_event_export(result.path, known_item_ids=set(), codec=ParquetCodec())
    watermark = _watermark(factory)
    assert watermark.last_event_id == 0
    assert watermark.expected_checksum == result.manifest_checksum


def test_official_snapshot_status_is_complete_for_event_training(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    sequence_ids = _seed_event_graph(
        factory,
        complete_events=0,
        official_snapshot_events=1,
    )
    with factory.begin() as session:
        result = TrainingEventExporter().export(session, output_root=tmp_path)
    accepted = ParquetCodec().read_rows(result.path / "events.parquet")
    assert [row["event_sequence_id"] for row in accepted] == sequence_ids
    assert (result.accepted, result.rejected) == (1, 0)
    validate_event_export(
        result.path,
        known_item_ids={"official-snapshot-item"},
        codec=ParquetCodec(),
    )


def test_late_client_time_and_missing_metadata_partition_without_loss(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    sequence_ids = _seed_event_graph(factory, complete_events=2, incomplete_events=1)
    with factory.begin() as session:
        result = TrainingEventExporter().export(session, output_root=tmp_path)
    manifest = json.loads((result.path / "manifest.json").read_bytes())
    accepted = ParquetCodec().read_rows(result.path / "events.parquet")
    rejected = TrainingEventExporter()._read_rejected(result.path / "rejected.parquet")
    assert [row["event_sequence_id"] for row in accepted] == sequence_ids[:2]
    assert [row["event_sequence_id"] for row in rejected] == sequence_ids[2:]
    assert set(row["event_sequence_id"] for row in accepted).isdisjoint(
        row["event_sequence_id"] for row in rejected
    )
    assert sorted([row["event_sequence_id"] for row in accepted + rejected]) == sequence_ids
    assert manifest["event_counts"] == {"accepted": 2, "rejected": 1, "total": 3}
    assert manifest["rejected_reason_counts"] == {"missing_item_metadata": 1}
    assert rejected[0]["reason"] == "missing_item_metadata"
    assert manifest["watermark"] == {"start_exclusive": 0, "end_inclusive": sequence_ids[-1]}
    validated_manifest, validated_accepted, validated_checksum = validate_event_export(
        result.path,
        known_item_ids={"complete-item"},
        codec=ParquetCodec(),
    )
    assert validated_manifest == manifest
    assert validated_accepted == accepted
    assert validated_checksum == result.manifest_checksum


def test_postgres_sequence_gap_preserves_complete_source_partition(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    sequence_ids = _seed_event_graph(
        factory,
        complete_events=2,
        incomplete_events=1,
        gap_after_event=1,
    )
    assert sequence_ids[1] == sequence_ids[0] + 2
    with factory.begin() as session:
        result = TrainingEventExporter().export(
            session,
            output_root=tmp_path,
            watermark_name="gap-stream",
        )
    accepted = ParquetCodec().read_rows(result.path / "events.parquet")
    rejected = TrainingEventExporter()._read_rejected(result.path / "rejected.parquet")
    assert [row["event_sequence_id"] for row in accepted + rejected] == sequence_ids
    manifest, validated_accepted, checksum = validate_event_export(
        result.path,
        known_item_ids={"complete-item"},
        codec=ParquetCodec(),
    )
    assert manifest["watermark"]["end_inclusive"] == sequence_ids[-1]
    assert validated_accepted == accepted
    assert checksum == result.manifest_checksum
    claimed = ExportRange(
        name="gap-stream",
        start_exclusive=result.start_exclusive,
        end_inclusive=result.end_inclusive,
    )
    with factory() as session:
        source_rows = TrainingExportRepository().events_with_metadata(session, claimed)
    with pytest.raises(EventExportError, match="coverage mismatch"):
        TrainingEventExporter._validate_partition(
            claimed,
            source_rows,
            accepted[1:],
            rejected,
        )


class _WriteFailureExporter(TrainingEventExporter):
    def _write_rejected(self, path: Path, rows: list[dict[str, object]]) -> int:
        raise OSError("injected write failure")


class _RenameFailureExporter(TrainingEventExporter):
    def _publish_directory(self, temporary: Path, final: Path) -> None:
        raise OSError("injected rename failure")


class _CASFailureRepository(TrainingExportRepository):
    def complete(self, session: Session, claimed, *, checksum: str) -> bool:
        return False


@pytest.mark.parametrize("exporter", [_WriteFailureExporter(), _RenameFailureExporter()])
def test_write_or_rename_failure_never_advances_watermark(
    factory: sessionmaker[Session], tmp_path: Path, exporter: TrainingEventExporter
) -> None:
    _seed_event_graph(factory, complete_events=1)
    with pytest.raises(OSError), factory.begin() as session:
        exporter.export(session, output_root=tmp_path)
    assert _watermark(factory) is None
    assert not [path for path in tmp_path.iterdir() if not path.name.startswith(".")]


def test_cas_failure_publishes_but_retry_reuses_and_advances_once(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    sequence_ids = _seed_event_graph(factory, complete_events=1)
    failing = TrainingEventExporter(repository=_CASFailureRepository())
    with pytest.raises(ExportWatermarkCASFailure), factory.begin() as session:
        failing.export(session, output_root=tmp_path)
    assert _watermark(factory) is None
    published = tmp_path / f"0-{sequence_ids[-1]}"
    checksum = sha256_file(published / "manifest.json")
    with factory.begin() as session:
        retry = TrainingEventExporter().export(session, output_root=tmp_path)
    assert retry.reused is True
    assert retry.manifest_checksum == checksum
    assert _watermark(factory).last_event_id == sequence_ids[-1]


def test_watermark_namespace_collision_preserves_first_export_and_rolls_back_second(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    sequence_ids = _seed_event_graph(factory, complete_events=1, incomplete_events=1)
    with factory.begin() as session:
        first = TrainingEventExporter().export(
            session,
            output_root=tmp_path,
            watermark_name="stream-one",
        )
    before = {
        path.relative_to(first.path): path.read_bytes()
        for path in first.path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ExportNamespaceCollision), factory.begin() as session:
        TrainingEventExporter().export(
            session,
            output_root=tmp_path,
            watermark_name="stream-two",
        )

    after = {
        path.relative_to(first.path): path.read_bytes()
        for path in first.path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert sha256_file(first.path / "manifest.json") == first.manifest_checksum
    assert not list(tmp_path.glob(".*.invalid-*"))
    assert not list(tmp_path.glob(".*.tmp-*"))
    first_watermark = _watermark(factory, "stream-one")
    assert first_watermark.last_event_id == sequence_ids[-1]
    assert first_watermark.status == "completed"
    assert first_watermark.expected_checksum == first.manifest_checksum
    assert _watermark(factory, "stream-two") is None


def test_output_root_rejects_symlink_leaf_and_ancestor(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    leaf_target = tmp_path / "leaf-target"
    leaf_target.mkdir()
    leaf_link = tmp_path / "leaf-link"
    leaf_link.symlink_to(leaf_target, target_is_directory=True)

    ancestor_target = tmp_path / "ancestor-target"
    ancestor_target.mkdir()
    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(ancestor_target, target_is_directory=True)

    for name, output_root in (
        ("leaf-stream", leaf_link),
        ("ancestor-stream", ancestor_link / "exports"),
    ):
        with (
            pytest.raises(EventExportError, match="must not be symlinks"),
            factory.begin() as session,
        ):
            TrainingEventExporter().export(
                session,
                output_root=output_root,
                watermark_name=name,
            )
        assert _watermark(factory, name) is None
    assert not (ancestor_target / "exports").exists()


def test_commit_failure_leaves_published_range_reusable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed_event_graph(factory, complete_events=1)
    session = factory()
    session.begin()
    first = TrainingEventExporter().export(session, output_root=tmp_path)

    def fail_commit(_session: Session) -> None:
        raise RuntimeError("injected commit failure")

    event.listen(session, "before_commit", fail_commit, once=True)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        session.commit()
    session.rollback()
    session.close()
    assert _watermark(factory) is None
    with factory.begin() as retry_session:
        retry = TrainingEventExporter().export(retry_session, output_root=tmp_path)
    assert retry.reused is True
    assert retry.manifest_checksum == first.manifest_checksum


def test_tampered_crash_artifact_is_rebuilt_deterministically(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed_event_graph(factory, complete_events=2)
    session = factory()
    session.begin()
    first = TrainingEventExporter().export(session, output_root=tmp_path)
    session.rollback()
    session.close()
    with (first.path / "events.parquet").open("ab") as handle:
        handle.write(b"tamper")
        handle.flush()
        os.fsync(handle.fileno())
    with factory.begin() as retry_session:
        retry = TrainingEventExporter().export(retry_session, output_root=tmp_path)
    assert retry.reused is False
    assert retry.manifest_checksum == first.manifest_checksum
    assert not list(tmp_path.glob(".*.invalid-*"))
    validate_event_export(
        retry.path,
        known_item_ids={"complete-item"},
        codec=ParquetCodec(),
    )


def test_concurrent_exporters_serialize_without_duplicates(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    sequence_ids = _seed_event_graph(factory, complete_events=3)
    barrier = threading.Barrier(2)
    results = []
    failures = []

    def execute() -> None:
        try:
            barrier.wait(timeout=5)
            with factory.begin() as session:
                results.append(TrainingEventExporter().export(session, output_root=tmp_path))
        except BaseException as exc:  # pragma: no cover - reported by parent assertion
            failures.append(exc)

    threads = [threading.Thread(target=execute), threading.Thread(target=execute)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result.accepted for result in results) == [0, 3]
    assert {(result.start_exclusive, result.end_inclusive) for result in results} == {
        (0, sequence_ids[-1]),
        (sequence_ids[-1], sequence_ids[-1]),
    }
    assert _watermark(factory).last_event_id == sequence_ids[-1]
    with factory() as session:
        persisted = session.scalar(select(TrainingExportWatermark))
        assert persisted.expected_checksum in {result.manifest_checksum for result in results}
