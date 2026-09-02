from __future__ import annotations

import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.security import JWTService, JWTSettings, PasswordService, normalize_username
from apps.api.app.auth.service import AuthService
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    Event,
    EventType,
    Exposure,
    FeedType,
    Item,
    ModelActivationAttempt,
    ModelStatus,
    ModelVersion,
    OnlineStatus,
    Operation,
    OperationBatch,
    Role,
    TrainingJob,
    TrainingJobStatus,
    User,
    UserProfile,
)
from apps.api.app.db.seed import SEED_USERS, seed_demo_users
from apps.api.app.events.schemas import EventBatchRequest, EventRequest
from apps.api.app.events.service import (
    EventService,
    PageExposure,
    SnapshotCandidate,
    SnapshotService,
)
from apps.api.app.models_registry.repository import ModelRegistryRepository
from apps.api.app.models_registry.service import ActivationService
from apps.api.app.operations.schemas import OperationBatchRequest
from apps.api.app.operations.service import AuditedOperationFailure, OperationService

NOW = datetime(2026, 8, 31, 12, 15, tzinfo=UTC)
PASSWORD = "Phase 2B isolated PostgreSQL password!"


class Loader:
    def stage(self, *, artifact_uri: str, artifact_checksum: str, manifest_checksum: str) -> object:
        return {
            "uri": artifact_uri,
            "artifact_checksum": artifact_checksum,
            "manifest_checksum": manifest_checksum,
        }


def model(version: str) -> ModelVersion:
    return ModelVersion(
        model_version=version,
        data_version="phase2b-test-data",
        config_checksum="a" * 64,
        metrics={"ndcg": 0.2},
        artifact_uri=f"{version}.bundle",
        artifact_checksum="b" * 64,
        manifest_checksum="c" * 64,
        purpose=EvaluationPurpose.BASE_OFFICIAL,
        evaluation_comparability=Comparability.COMPARABLE,
        activation_eligible=True,
        status=ModelStatus.READY,
        trained_at=NOW,
    )


@unittest.skipUnless(
    os.environ.get("PHASE2B_TEST_DATABASE_URL"),
    "set PHASE2B_TEST_DATABASE_URL to an isolated migrated PostgreSQL database",
)
class PostgreSQLCoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ["PHASE2B_TEST_DATABASE_URL"]
        cls.engine = create_engine(database_url, pool_pre_ping=True)
        cls.factory = sessionmaker(
            bind=cls.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_migrated_core_transactions(self) -> None:
        passwords = PasswordService()
        tokens = JWTService(JWTSettings(secret="phase2b-test-jwt-secret-with-at-least-32-bytes"))
        auth = AuthService(passwords, tokens)

        with self.factory.begin() as session:
            self.assertEqual(
                session.scalar(text("SELECT version_num FROM alembic_version")), "20260902_0005"
            )
            first = seed_demo_users(
                session,
                password=PASSWORD,
                hash_password=passwords.hash,
                normalize_username=normalize_username,
            )
        with self.factory.begin() as session:
            second = seed_demo_users(
                session,
                password=PASSWORD,
                hash_password=passwords.hash,
                normalize_username=normalize_username,
            )
            self.assertEqual([user.id for user in first], [user.id for user in second])
            seed_names = [username for username, _role in SEED_USERS]
            self.assertEqual(
                session.scalar(
                    select(func.count(User.id)).where(User.username_normalized.in_(seed_names))
                ),
                6,
            )
            self.assertEqual(
                set(
                    session.scalars(
                        select(User.role).where(User.username_normalized.in_(seed_names))
                    )
                ),
                set(Role),
            )

        with self.factory.begin() as session:
            user, issued = auth.login(session, "DEMO_USER_A", PASSWORD, now=NOW)
            user_id = user.id
            session.add_all([Item(id="pg-item-1", title="One"), Item(id="pg-item-2", title="Two")])
        with self.factory.begin() as session:
            authenticated = auth.authenticate(session, issued.token, now=NOW + timedelta(minutes=1))
            self.assertEqual(authenticated.user.id, user_id)
            auth.revoke(session, authenticated, now=NOW + timedelta(minutes=2))
        with self.factory() as session:
            with self.assertRaisesRegex(Exception, "invalid or expired"):
                auth.authenticate(session, issued.token, now=NOW + timedelta(minutes=3))

        snapshots = SnapshotService()
        with self.factory.begin() as session:
            snapshot = snapshots.create_snapshot(
                session,
                user_id=user_id,
                feed_type=FeedType.PERSONALIZED,
                model_version="pg-model",
                snapshot_seed=7,
                expires_at=NOW + timedelta(hours=1),
                candidates=[
                    SnapshotCandidate("pg-item-1", "dssm", 1.0, 1.0, 0),
                    SnapshotCandidate("pg-item-2", "popular", 0.5, 0.5, 1),
                ],
                now=NOW,
            )
            snapshot_id = snapshot.snapshot_id
        with self.factory.begin() as session:
            page = snapshots.record_page(
                session,
                snapshot_id=snapshot_id,
                user_id=user_id,
                offset=0,
                limit=2,
                latency_ms=3,
                page=[
                    PageExposure("pg-item-1", 0, "dssm"),
                    PageExposure("pg-item-2", 1, "popular"),
                ],
                now=NOW,
            )
            request_id = page.request_id

        accepted = EventRequest.model_validate(
            {
                "event_id": uuid.uuid4(),
                "request_id": request_id,
                "item_id": "pg-item-1",
                "position": 0,
                "event_type": "click",
                "client_timestamp": NOW,
                "payload": {"source": "postgres-integration"},
            }
        )
        forged = EventRequest.model_validate(
            {
                "event_id": uuid.uuid4(),
                "request_id": request_id,
                "item_id": "pg-item-2",
                "position": 0,
                "event_type": "like",
                "client_timestamp": NOW,
            }
        )
        batch = EventBatchRequest(batch_id=uuid.uuid4(), events=[accepted, forged, accepted])
        with self.factory.begin() as session:
            result = EventService().submit_batch(session, user_id=user_id, request=batch, now=NOW)
            self.assertEqual((result.accepted, result.duplicate, result.rejected), (1, 1, 1))
        with self.factory() as session:
            self.assertEqual(session.get(UserProfile, user_id).profile_version, 1)
            self.assertEqual(
                session.scalar(select(func.count(Event.id)).where(Event.request_id == request_id)),
                3,
            )

        operator_id = next(user.id for user in first if user.role == Role.OPERATOR)
        operation_request = OperationBatchRequest.model_validate(
            {
                "batch_id": uuid.uuid4(),
                "operation_type": "offline",
                "targets": ["pg-item-1", "pg-item-2"],
                "scope_type": "all",
                "starts_at_utc": NOW,
                "ends_at_utc": NOW + timedelta(hours=1),
                "priority": 0,
                "reason": "PostgreSQL atomicity integration",
                "semantics": "preflight_then_all_or_nothing_transaction",
            }
        )
        calls = 0

        def fail_second(_item_id: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected")

        with self.factory() as session:
            with self.assertRaises(AuditedOperationFailure):
                OperationService().create_batch(
                    session,
                    operator_id=operator_id,
                    request=operation_request,
                    now=NOW,
                    before_each_apply=fail_second,
                )
            session.commit()
        with self.factory() as session:
            self.assertEqual(session.get(Item, "pg-item-1").online_status, OnlineStatus.ONLINE)
            self.assertEqual(session.get(Item, "pg-item-2").online_status, OnlineStatus.ONLINE)
            self.assertEqual(
                session.scalar(
                    select(func.count(Operation.id)).where(
                        Operation.batch_id == operation_request.batch_id,
                        Operation.result == "failed",
                    )
                ),
                2,
            )

        activation = ActivationService(publish_token="p" * 32, loader=Loader())
        with self.factory.begin() as session:
            session.add_all([model("pg-v1"), model("pg-v2"), model("pg-v3")])
        for version, expected in [("pg-v1", None), ("pg-v2", "pg-v1")]:
            with self.factory() as session:
                prepared = activation.prepare(session, version=version, manifest_checksum="c" * 64)
                session.rollback()
                with session.begin():
                    activation.activate_prepared(
                        session,
                        prepared=prepared,
                        expected_current_version=expected,
                        now=NOW,
                    )
        with self.factory() as session:
            active = list(
                session.scalars(
                    select(ModelVersion.model_version).where(
                        ModelVersion.status == ModelStatus.ACTIVE
                    )
                )
            )
            self.assertEqual(active, ["pg-v2"])

        with self.factory() as session:
            attempt = activation.begin_attempt(
                session,
                version="pg-v3",
                expected_current_version="stale",
                now=NOW,
            )
            prepared = activation.prepare(session, version="pg-v3", manifest_checksum="c" * 64)
            session.commit()
            try:
                with session.begin():
                    activation.activate_prepared(
                        session,
                        prepared=prepared,
                        expected_current_version="stale",
                        attempt_id=attempt.id,
                        now=NOW,
                    )
            except ApiError as exc:
                with session.begin():
                    activation.record_failure(
                        session,
                        attempt_id=attempt.id,
                        code=exc.code,
                        reason=exc.message,
                        now=NOW,
                    )
            else:
                self.fail("expected PostgreSQL activation CAS conflict")
        with self.factory() as session:
            self.assertEqual(session.get(ModelVersion, "pg-v2").status, ModelStatus.ACTIVE)
            self.assertEqual(session.get(ModelVersion, "pg-v3").status, ModelStatus.READY)
            self.assertEqual(
                session.get(ModelActivationAttempt, attempt.id).failure_code,
                "activation_cas_conflict",
            )

    def _event_fixture(self, prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
        user_id = uuid.uuid4()
        with self.factory.begin() as session:
            session.add(
                User(
                    id=user_id,
                    username=f"{prefix}-user",
                    username_normalized=f"{prefix}-user",
                    password_hash="isolated-test-hash",
                    role=Role.USER,
                    status="enabled",
                )
            )
            session.add(UserProfile(user_id=user_id))
            session.add_all(
                [
                    Item(id=f"{prefix}-item-1", title="Alpha Topic"),
                    Item(id=f"{prefix}-item-2", title="Beta Topic"),
                ]
            )
        snapshots = SnapshotService()
        with self.factory.begin() as session:
            snapshot = snapshots.create_snapshot(
                session,
                user_id=user_id,
                feed_type=FeedType.PERSONALIZED,
                model_version="concurrency-model",
                snapshot_seed=13,
                expires_at=NOW + timedelta(hours=1),
                candidates=[
                    SnapshotCandidate(f"{prefix}-item-1", "dssm", 1.0, 1.0, 0),
                    SnapshotCandidate(f"{prefix}-item-2", "popular", 0.5, 0.5, 1),
                ],
                now=NOW,
            )
        with self.factory.begin() as session:
            page = snapshots.record_page(
                session,
                snapshot_id=snapshot.snapshot_id,
                user_id=user_id,
                offset=0,
                limit=2,
                latency_ms=1,
                page=[
                    PageExposure(f"{prefix}-item-1", 0, "dssm"),
                    PageExposure(f"{prefix}-item-2", 1, "popular"),
                ],
                now=NOW,
            )
        return user_id, page.request_id

    def test_database_constraints_reject_service_bypass(self) -> None:
        prefix = f"fk-{uuid.uuid4().hex[:8]}"
        user_id, request_id = self._event_fixture(prefix)
        with self.factory() as session:
            exposure = session.scalar(
                select(Exposure).where(
                    Exposure.request_id == request_id,
                    Exposure.item_id == f"{prefix}-item-1",
                )
            )
            session.add(
                Event(
                    event_id=uuid.uuid4(),
                    exposure_id=exposure.id,
                    request_id=request_id,
                    user_id=user_id,
                    item_id=f"{prefix}-item-2",
                    position=1,
                    feed_type=FeedType.PERSONALIZED,
                    source="popular",
                    event_type=EventType.CLICK,
                    client_timestamp=NOW,
                    server_timestamp=NOW,
                    duration_ms=None,
                    payload={},
                    payload_hash="f" * 64,
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

        invalid_jobs = [
            TrainingJob(
                idempotency_key=f"{prefix}-systems",
                data_version="data",
                data_manifest_checksum="d" * 64,
                config_checksum="e" * 64,
                purpose=EvaluationPurpose.SYSTEMS_ONLY,
                evaluation_comparability=Comparability.COMPARABLE,
                activation_eligible=False,
                status=TrainingJobStatus.QUEUED,
                created_at=NOW,
            ),
            TrainingJob(
                idempotency_key=f"{prefix}-eligible",
                data_version="data",
                data_manifest_checksum="d" * 64,
                config_checksum="e" * 64,
                purpose=EvaluationPurpose.BASE_OFFICIAL,
                evaluation_comparability=Comparability.NON_COMPARABLE,
                activation_eligible=True,
                status=TrainingJobStatus.QUEUED,
                created_at=NOW,
            ),
        ]
        for job in invalid_jobs:
            with self.subTest(key=job.idempotency_key), self.factory() as session:
                session.add(job)
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_concurrent_idempotency_claims_have_one_side_effect(self) -> None:
        prefix = f"race-{uuid.uuid4().hex[:8]}"
        user_id, request_id = self._event_fixture(prefix)
        event = EventRequest.model_validate(
            {
                "event_id": uuid.uuid4(),
                "request_id": request_id,
                "item_id": f"{prefix}-item-1",
                "position": 0,
                "event_type": "click",
                "client_timestamp": NOW,
                "payload": {"race": "same"},
            }
        )

        event_barrier = Barrier(2)

        def submit_event() -> str:
            with self.factory() as session:
                event_barrier.wait()
                result = EventService().submit(session, user_id=user_id, request=event, now=NOW)
                session.commit()
                return result.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(executor.map(lambda _index: submit_event(), range(2)))
        self.assertEqual(statuses, ["accepted", "duplicate"])
        with self.factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count(Event.id)).where(Event.event_id == event.event_id)
                ),
                1,
            )
            self.assertEqual(session.get(UserProfile, user_id).profile_version, 1)
        with self.factory() as session:
            conflict = EventService().submit(
                session,
                user_id=user_id,
                request=event.model_copy(update={"payload": {"race": "different"}}),
                now=NOW,
            )
            self.assertEqual(conflict.error_code, "event_id_conflict")

        batch_event = event.model_copy(update={"event_id": uuid.uuid4(), "event_type": "like"})
        batch = EventBatchRequest(batch_id=uuid.uuid4(), events=[batch_event])
        batch_barrier = Barrier(2)

        def submit_batch() -> object:
            with self.factory() as session:
                batch_barrier.wait()
                response = EventService().submit_batch(
                    session, user_id=user_id, request=batch, now=NOW
                )
                session.commit()
                return response

        with ThreadPoolExecutor(max_workers=2) as executor:
            batch_results = list(executor.map(lambda _index: submit_batch(), range(2)))
        self.assertEqual(batch_results[0], batch_results[1])
        with self.factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count(Event.id)).where(Event.event_id == batch_event.event_id)
                ),
                1,
            )
            self.assertEqual(session.get(UserProfile, user_id).profile_version, 2)
            conflicting_batch = batch.model_copy(
                update={"events": [batch_event.model_copy(update={"payload": {"different": True}})]}
            )
            with self.assertRaises(ApiError) as raised:
                EventService().submit_batch(
                    session, user_id=user_id, request=conflicting_batch, now=NOW
                )
            self.assertEqual(raised.exception.status_code, 409)

        job_key = f"{prefix}-job"
        job_barrier = Barrier(2)

        def enqueue_job() -> uuid.UUID:
            job = TrainingJob(
                idempotency_key=job_key,
                data_version="data-v1",
                data_manifest_checksum="d" * 64,
                config_checksum="e" * 64,
                purpose=EvaluationPurpose.BASE_OFFICIAL,
                evaluation_comparability=Comparability.COMPARABLE,
                activation_eligible=True,
                status=TrainingJobStatus.QUEUED,
                created_at=NOW,
            )
            with self.factory() as session:
                job_barrier.wait()
                claimed = ModelRegistryRepository().enqueue_job(session, job)
                session.commit()
                return claimed.job_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            job_ids = list(executor.map(lambda _index: enqueue_job(), range(2)))
        self.assertEqual(job_ids[0], job_ids[1])
        with self.factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count(TrainingJob.job_id)).where(
                        TrainingJob.idempotency_key == job_key
                    )
                ),
                1,
            )
            conflict_job = TrainingJob(
                idempotency_key=job_key,
                data_version="different",
                data_manifest_checksum="d" * 64,
                config_checksum="e" * 64,
                purpose=EvaluationPurpose.BASE_OFFICIAL,
                evaluation_comparability=Comparability.COMPARABLE,
                activation_eligible=True,
                status=TrainingJobStatus.QUEUED,
                created_at=NOW,
            )
            with self.assertRaises(ApiError) as raised:
                ModelRegistryRepository().enqueue_job(session, conflict_job)
            self.assertEqual(raised.exception.status_code, 409)

        operator_id = uuid.uuid4()
        operation_item = f"{prefix}-operation-item"
        with self.factory.begin() as session:
            session.add(
                User(
                    id=operator_id,
                    username=f"{prefix}-operator",
                    username_normalized=f"{prefix}-operator",
                    password_hash="isolated-test-hash",
                    role=Role.OPERATOR,
                    status="enabled",
                )
            )
            session.add(Item(id=operation_item, title="Operation race"))
        operation_request = OperationBatchRequest.model_validate(
            {
                "batch_id": uuid.uuid4(),
                "operation_type": "offline",
                "targets": [operation_item],
                "scope_type": "all",
                "starts_at_utc": NOW,
                "ends_at_utc": NOW + timedelta(hours=1),
                "priority": 0,
                "reason": "concurrent idempotency",
                "semantics": "preflight_then_all_or_nothing_transaction",
            }
        )
        operation_barrier = Barrier(2)

        def create_operation() -> object:
            with self.factory() as session:
                operation_barrier.wait()
                response = OperationService().create_batch(
                    session,
                    operator_id=operator_id,
                    request=operation_request,
                    now=NOW,
                )
                session.commit()
                return response

        with ThreadPoolExecutor(max_workers=2) as executor:
            operation_results = list(executor.map(lambda _index: create_operation(), range(2)))
        self.assertEqual(operation_results[0], operation_results[1])
        with self.factory() as session:
            self.assertEqual(session.get(Item, operation_item).state_version, 1)
            self.assertEqual(
                session.scalar(
                    select(func.count(Operation.id)).where(
                        Operation.batch_id == operation_request.batch_id
                    )
                ),
                1,
            )
            self.assertEqual(
                session.get(OperationBatch, operation_request.batch_id).operator_role,
                Role.OPERATOR,
            )
            conflicting_operation = operation_request.model_copy(
                update={"reason": "different concurrent payload"}
            )
            with self.assertRaises(ApiError) as raised:
                OperationService().create_batch(
                    session,
                    operator_id=operator_id,
                    request=conflicting_operation,
                    now=NOW,
                )
            self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
