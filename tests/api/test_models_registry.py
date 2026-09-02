from __future__ import annotations

import unittest
import uuid

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from apps.api.app.auth.errors import ApiError
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    ModelActivationAttempt,
    ModelStatus,
    ModelVersion,
    TrainingJob,
    TrainingJobStatus,
)
from apps.api.app.models_registry.repository import ModelRegistryRepository
from apps.api.app.models_registry.service import ActivationService

from ._support import NOW, factory_for, sqlite_engine


def model(version: str, status: ModelStatus = ModelStatus.READY) -> ModelVersion:
    return ModelVersion(
        model_version=version,
        data_version="data-v1",
        config_checksum="a" * 64,
        metrics={"ndcg": 0.2},
        artifact_uri=f"{version}.bundle",
        artifact_checksum="b" * 64,
        manifest_checksum="c" * 64,
        purpose=EvaluationPurpose.BASE_OFFICIAL,
        evaluation_comparability=Comparability.COMPARABLE,
        activation_eligible=True,
        status=status,
        trained_at=NOW,
    )


class Loader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def stage(self, *, artifact_uri: str, artifact_checksum: str, manifest_checksum: str) -> object:
        self.calls += 1
        if self.fail:
            raise ValueError("load failed")
        return {"uri": artifact_uri, "checksum": artifact_checksum, "manifest": manifest_checksum}


class ModelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.repository = ModelRegistryRepository()
        with self.factory.begin() as session:
            session.add_all([model("v1"), model("v2"), model("v3")])

    def tearDown(self) -> None:
        self.engine.dispose()

    def _activate(self, service: ActivationService, version: str, expected: str | None) -> object:
        with self.factory() as session:
            prepared = service.prepare(session, version=version, manifest_checksum="c" * 64)
            session.rollback()
            with session.begin():
                plan = service.activate_prepared(
                    session,
                    prepared=prepared,
                    expected_current_version=expected,
                    now=NOW,
                )
            return plan

    def test_token_staging_cas_and_single_active_switch(self) -> None:
        loader = Loader()
        service = ActivationService(publish_token="p" * 32, loader=loader)
        with self.assertRaises(ApiError):
            service.authenticate_publish_token("wrong")
        service.authenticate_publish_token("p" * 32)
        self._activate(service, "v1", None)
        with self.factory() as session:
            self.assertEqual(session.get(ModelVersion, "v1").status, ModelStatus.ACTIVE)
        self._activate(service, "v2", "v1")
        with self.factory() as session:
            self.assertEqual(session.get(ModelVersion, "v1").status, ModelStatus.ARCHIVED)
            self.assertEqual(session.get(ModelVersion, "v2").status, ModelStatus.ACTIVE)
            self.assertEqual(
                list(
                    session.scalars(
                        select(ModelVersion.model_version).where(
                            ModelVersion.status == ModelStatus.ACTIVE
                        )
                    )
                ),
                ["v2"],
            )

        with self.assertRaisesRegex(ApiError, "does not match"):
            self._activate(service, "v3", "stale-version")
        with self.factory() as session:
            self.assertEqual(session.get(ModelVersion, "v2").status, ModelStatus.ACTIVE)
            self.assertEqual(session.get(ModelVersion, "v3").status, ModelStatus.READY)

    def test_active_switch_flushes_archive_before_activation(self) -> None:
        service = ActivationService(publish_token="p" * 32, loader=Loader())
        self._activate(service, "v1", None)
        flush_states: list[dict[str, ModelStatus]] = []
        with self.factory() as session:
            prepared = service.prepare(session, version="v2", manifest_checksum="c" * 64)
            session.rollback()

            def capture_flush(current_session, _context, _instances) -> None:
                flush_states.append(
                    {
                        model_row.model_version: model_row.status
                        for model_row in current_session.dirty
                        if isinstance(model_row, ModelVersion)
                    }
                )

            event.listen(session, "before_flush", capture_flush)
            try:
                with session.begin():
                    service.activate_prepared(
                        session,
                        prepared=prepared,
                        expected_current_version="v1",
                        now=NOW,
                    )
            finally:
                event.remove(session, "before_flush", capture_flush)

        self.assertGreaterEqual(len(flush_states), 2)
        self.assertEqual(flush_states[0], {"v1": ModelStatus.ARCHIVED})
        self.assertEqual(flush_states[1], {"v2": ModelStatus.ACTIVE})

    def test_staging_failure_and_systems_only_invariant_preserve_active(self) -> None:
        good = ActivationService(publish_token="p" * 32, loader=Loader())
        self._activate(good, "v1", None)
        failing = ActivationService(publish_token="p" * 32, loader=Loader(fail=True))
        with self.factory() as session:
            with self.assertRaisesRegex(ApiError, "staging"):
                failing.prepare(session, version="v2", manifest_checksum="c" * 64)
        with self.factory() as session:
            self.assertEqual(session.get(ModelVersion, "v1").status, ModelStatus.ACTIVE)
            self.assertEqual(session.get(ModelVersion, "v2").status, ModelStatus.READY)

        invalid = model("systems")
        invalid.purpose = EvaluationPurpose.SYSTEMS_ONLY
        invalid.evaluation_comparability = Comparability.NON_COMPARABLE
        invalid.activation_eligible = False
        with self.factory.begin() as session:
            with self.assertRaisesRegex(ValueError, "systems_only"):
                self.repository.add_model(session, invalid)

    def test_training_job_idempotency_conflict_and_database_eligibility_constraints(self) -> None:
        def job(**updates: object) -> TrainingJob:
            values = {
                "job_id": uuid.uuid4(),
                "idempotency_key": "train-key",
                "data_version": "data-v1",
                "data_manifest_checksum": "d" * 64,
                "config_checksum": "e" * 64,
                "purpose": EvaluationPurpose.BASE_OFFICIAL,
                "evaluation_comparability": Comparability.COMPARABLE,
                "activation_eligible": True,
                "status": TrainingJobStatus.QUEUED,
                "created_at": NOW,
            }
            values.update(updates)
            return TrainingJob(**values)

        with self.factory.begin() as session:
            first = self.repository.enqueue_job(session, job())
        with self.factory.begin() as session:
            replay = self.repository.enqueue_job(session, job())
            self.assertEqual(replay.job_id, first.job_id)
        with self.factory() as session, self.assertRaises(ApiError) as raised:
            self.repository.enqueue_job(session, job(data_version="different"))
        self.assertEqual(raised.exception.status_code, 409)

        invalid_jobs = [
            job(
                idempotency_key="systems-invalid",
                purpose=EvaluationPurpose.SYSTEMS_ONLY,
                evaluation_comparability=Comparability.COMPARABLE,
                activation_eligible=False,
            ),
            job(
                idempotency_key="eligible-invalid",
                evaluation_comparability=Comparability.NON_COMPARABLE,
                activation_eligible=True,
            ),
        ]
        for invalid_job in invalid_jobs:
            with self.subTest(key=invalid_job.idempotency_key):
                with self.factory() as session, self.assertRaises(IntegrityError):
                    session.add(invalid_job)
                    session.flush()

    def test_activation_failures_are_audited_and_preserve_old_active(self) -> None:
        good = ActivationService(publish_token="p" * 32, loader=Loader())
        self._activate(good, "v1", None)

        def record_prepare_failure(
            service: ActivationService, *, token: str, checksum: str, expected: str | None
        ) -> None:
            with self.factory() as session:
                attempt = service.begin_attempt(
                    session,
                    version="v2",
                    expected_current_version=expected,
                    now=NOW,
                )
                try:
                    service.authenticate_publish_token(token)
                    service.prepare(session, version="v2", manifest_checksum=checksum)
                except ApiError as exc:
                    service.record_failure(
                        session,
                        attempt_id=attempt.id,
                        code=exc.code,
                        reason=exc.message,
                        now=NOW,
                    )
                    session.commit()
                    return
            self.fail("expected activation preparation failure")

        record_prepare_failure(good, token="wrong", checksum="c" * 64, expected="v1")
        record_prepare_failure(good, token="p" * 32, checksum="0" * 64, expected="v1")
        record_prepare_failure(
            ActivationService(publish_token="p" * 32, loader=Loader(fail=True)),
            token="p" * 32,
            checksum="c" * 64,
            expected="v1",
        )

        with self.factory() as session:
            attempt = good.begin_attempt(
                session,
                version="v2",
                expected_current_version="stale",
                now=NOW,
            )
            prepared = good.prepare(session, version="v2", manifest_checksum="c" * 64)
            session.commit()
            try:
                with session.begin():
                    good.activate_prepared(
                        session,
                        prepared=prepared,
                        expected_current_version="stale",
                        attempt_id=attempt.id,
                        now=NOW,
                    )
            except ApiError as exc:
                with session.begin():
                    good.record_failure(
                        session,
                        attempt_id=attempt.id,
                        code=exc.code,
                        reason=exc.message,
                        now=NOW,
                    )
            else:
                self.fail("expected activation CAS conflict")

        with self.factory() as session:
            self.assertEqual(session.get(ModelVersion, "v1").status, ModelStatus.ACTIVE)
            self.assertEqual(session.get(ModelVersion, "v2").status, ModelStatus.READY)
            attempts = list(
                session.scalars(
                    select(ModelActivationAttempt).order_by(ModelActivationAttempt.created_at)
                )
            )
            self.assertEqual(len(attempts), 4)
            self.assertTrue(all(attempt.status == "failed" for attempt in attempts))
            self.assertEqual(attempts[-1].failure_code, "activation_cas_conflict")
            self.assertIn("activation_cas_conflict", session.get(ModelVersion, "v2").failure_reason)


if __name__ == "__main__":
    unittest.main()
