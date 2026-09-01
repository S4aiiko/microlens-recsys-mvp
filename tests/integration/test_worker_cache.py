from __future__ import annotations

import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select

from apps.api.app.auth.errors import ApiError
from apps.api.app.cache import (
    AuthorityDenied,
    CacheAuthority,
    CachePolicy,
    InMemoryCacheBackend,
    VersionedCache,
)
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    Item,
    JobAttempt,
    Operation,
    OperationBatch,
    PromotionRule,
    PromotionStatus,
    Role,
    TrainingJob,
    TrainingJobStatus,
)
from apps.api.app.operations.schemas import OperationBatchRequest
from apps.api.app.operations.service import OperationService
from apps.worker import app as worker_app
from apps.worker.contracts import PermanentTrainingError, RetryableTrainingError
from apps.worker.jobs import JobCoordinator
from apps.worker.operations import ScheduledOperationsRunner
from apps.worker.runtime import WorkerRuntime
from tests.api._support import add_user, factory_for, sqlite_engine

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class FloatClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class DateClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FailingCacheBackend:
    def get(self, key: str) -> bytes | None:
        raise ConnectionError("redis disconnected")

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        raise ConnectionError("redis disconnected")

    def delete(self, key: str) -> None:
        raise ConnectionError("redis disconnected")

    def ping(self) -> bool:
        raise ConnectionError("redis disconnected")


class CacheRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FloatClock()
        self.backend = InMemoryCacheBackend(monotonic=self.clock)
        self.cache = VersionedCache(
            self.backend,
            policy=CachePolicy(ttl_seconds=10, process_fallback_ttl_seconds=2),
            monotonic=self.clock,
        )
        self.loads = 0

    def authority(self, **updates: Any) -> CacheAuthority:
        values = {
            "profile_version": 1,
            "active_model_version": "model-v1",
            "operations_generation": 1,
            "permission_generation": 1,
            "allowed": True,
            "online": True,
        }
        values.update(updates)
        return CacheAuthority(**values)

    def load(self) -> dict[str, int]:
        self.loads += 1
        return {"load": self.loads}

    def test_ttl_generation_invalidation_and_metrics(self) -> None:
        get = lambda authority: self.cache.get_or_load(  # noqa: E731
            resource="feed:user-a", authority=lambda: authority, loader=self.load
        )
        self.assertEqual(get(self.authority()), {"load": 1})
        self.assertEqual(get(self.authority()), {"load": 1})
        self.assertEqual(get(self.authority(profile_version=2)), {"load": 2})
        self.assertEqual(get(self.authority(active_model_version="model-v2")), {"load": 3})
        self.assertEqual(get(self.authority(operations_generation=2)), {"load": 4})

        current = self.authority(operations_generation=2)
        self.cache.invalidate(resource="feed:user-a", authority=current)
        self.assertEqual(get(current), {"load": 5})
        self.clock.value += 11
        self.assertEqual(get(current), {"load": 6})
        metrics = self.cache.metrics.snapshot()
        self.assertEqual(metrics["hits"], 1)
        self.assertEqual(metrics["loads"], 6)
        self.assertEqual(metrics["invalidations"], 1)

    def test_offline_and_permission_authority_precede_old_cache(self) -> None:
        self.cache.get_or_load(
            resource="item:item-1", authority=lambda: self.authority(), loader=self.load
        )
        with self.assertRaises(AuthorityDenied):
            self.cache.get_or_load(
                resource="item:item-1",
                authority=lambda: self.authority(online=False),
                loader=self.load,
            )
        with self.assertRaises(AuthorityDenied):
            self.cache.get_or_load(
                resource="item:item-1",
                authority=lambda: self.authority(allowed=False),
                loader=self.load,
            )
        self.assertEqual(self.loads, 1)

    def test_redis_disconnect_uses_bounded_process_fallback_then_loader(self) -> None:
        cache = VersionedCache(
            FailingCacheBackend(),
            policy=CachePolicy(ttl_seconds=10, process_fallback_ttl_seconds=2),
            monotonic=self.clock,
        )

        def authority() -> CacheAuthority:
            return self.authority()

        self.assertEqual(
            cache.get_or_load(resource="profile:user-a", authority=authority, loader=self.load),
            {"load": 1},
        )
        self.assertEqual(
            cache.get_or_load(resource="profile:user-a", authority=authority, loader=self.load),
            {"load": 1},
        )
        self.clock.value += 3
        self.assertEqual(
            cache.get_or_load(resource="profile:user-a", authority=authority, loader=self.load),
            {"load": 2},
        )
        self.assertEqual(cache.metrics.process_fallback_hits, 1)
        self.assertGreaterEqual(cache.metrics.backend_failures, 3)


class FakeBroker:
    def __init__(self) -> None:
        self.messages: list[uuid.UUID] = []
        self.fail = False

    def notify(self, job_id: uuid.UUID) -> None:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.messages.append(job_id)

    def receive(self, *, timeout_seconds: int = 0) -> uuid.UUID | None:
        if self.fail:
            raise ConnectionError("redis unavailable")
        return self.messages.pop(0) if self.messages else None

    def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("redis unavailable")
        return True


def job(key: str, *, created_at: datetime = NOW, systems_only: bool = False) -> TrainingJob:
    return TrainingJob(
        job_id=uuid.uuid4(),
        idempotency_key=key,
        data_version="data-v1",
        data_manifest_checksum="a" * 64,
        config_checksum="b" * 64,
        purpose=(
            EvaluationPurpose.SYSTEMS_ONLY if systems_only else EvaluationPurpose.BASE_OFFICIAL
        ),
        evaluation_comparability=(
            Comparability.NON_COMPARABLE if systems_only else Comparability.COMPARABLE
        ),
        activation_eligible=not systems_only,
        status=TrainingJobStatus.QUEUED,
        created_at=created_at,
    )


class SuccessHandler:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request, control):
        self.calls += 1
        control.heartbeat()
        return {"model_version": f"model-{request.job_id}", "model_status": "EVALUATED"}


class RetryOnceHandler(SuccessHandler):
    def __call__(self, request, control):
        self.calls += 1
        if self.calls == 1:
            raise RetryableTrainingError("transient failure")
        return {"model_version": f"model-{request.job_id}", "model_status": "EVALUATED"}


class FailFirstHandler:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request, control):
        self.calls += 1
        if self.calls == 1:
            raise PermanentTrainingError("bad job is isolated")
        return {"model_version": f"model-{request.job_id}", "model_status": "EVALUATED"}


class WorkerJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.broker = FakeBroker()
        self.coordinator = JobCoordinator(
            self.factory, broker=self.broker, lease_seconds=30, max_attempts=3
        )
        self.clock = DateClock()

    def tearDown(self) -> None:
        self.engine.dispose()

    def runtime(
        self,
        handler,
        *,
        handler_configured: bool = True,
        require_training_handler: bool = False,
    ) -> WorkerRuntime:
        return WorkerRuntime(
            coordinator=self.coordinator,
            handler=handler,
            worker_id="worker-a",
            clock=self.clock,
            scheduled_operations=ScheduledOperationsRunner(self.factory, clock=self.clock),
            handler_configured=handler_configured,
            require_training_handler=require_training_handler,
        )

    def test_duplicate_enqueue_delivery_and_payload_conflict(self) -> None:
        first = self.coordinator.enqueue(job("same-key"))
        replay = self.coordinator.enqueue(job("same-key"))
        self.assertEqual(first.job_id, replay.job_id)
        self.broker.messages.append(first.job_id)
        handler = SuccessHandler()
        runtime = self.runtime(handler)
        self.assertEqual(runtime.run_once()["job"], "succeeded")
        self.assertEqual(runtime.run_once()["job"], "idle")
        self.assertEqual(handler.calls, 1)
        with self.assertRaises(ApiError) as raised:
            conflict = job("same-key")
            conflict.data_version = "data-v2"
            self.coordinator.enqueue(conflict)
        self.assertEqual(raised.exception.status_code, 409)

    def test_lease_expiry_crash_recovery_creates_new_attempt(self) -> None:
        queued = self.coordinator.enqueue(job("crash-key"))
        first = self.coordinator.claim_next(worker_id="crashed", now=self.clock())
        self.assertIsNotNone(first)
        self.clock.value += timedelta(seconds=31)
        recovered = self.coordinator.claim_next(worker_id="worker-a", now=self.clock())
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.request.job_id, queued.job_id)
        self.assertEqual(recovered.request.attempt, 2)
        with self.factory() as session:
            attempts = list(
                session.scalars(
                    select(JobAttempt)
                    .where(JobAttempt.job_id == queued.job_id)
                    .order_by(JobAttempt.attempt)
                )
            )
            self.assertEqual([attempt.status for attempt in attempts], ["lease_expired", "running"])

    def test_retry_cancel_and_broker_disconnect_db_fallback(self) -> None:
        queued = self.coordinator.enqueue(job("retry-key"))
        handler = RetryOnceHandler()
        runtime = self.runtime(handler)
        self.assertEqual(runtime.run_once()["job"], "queued")
        self.assertEqual(runtime.run_once()["job"], "succeeded")
        self.assertEqual(handler.calls, 2)

        cancelled = self.coordinator.enqueue(
            job("cancel-key", created_at=NOW + timedelta(seconds=1))
        )
        self.assertEqual(
            self.coordinator.cancel(job_id=cancelled.job_id, now=self.clock()),
            TrainingJobStatus.CANCELLED,
        )
        self.broker.fail = True
        fallback = self.coordinator.enqueue(
            job("fallback-key", created_at=NOW + timedelta(seconds=2))
        )
        result = runtime.run_once()
        self.assertTrue(result["broker_degraded"])
        self.assertEqual(result["job_id"], str(fallback.job_id))
        readiness = runtime.readiness()
        self.assertTrue(readiness.ready)
        self.assertFalse(readiness.redis)
        self.assertEqual(readiness.status, "ready_degraded")

        with self.factory() as session:
            self.assertEqual(
                session.get(TrainingJob, queued.job_id).status, TrainingJobStatus.SUCCEEDED
            )
            self.assertEqual(
                session.get(TrainingJob, cancelled.job_id).status,
                TrainingJobStatus.CANCELLED,
            )

    def test_failure_isolation_and_systems_only_never_publishes(self) -> None:
        first = self.coordinator.enqueue(job("bad", created_at=NOW))
        second = self.coordinator.enqueue(job("good", created_at=NOW + timedelta(seconds=1)))
        runtime = self.runtime(FailFirstHandler())
        self.assertEqual(runtime.run_once()["job"], "failed")
        self.assertEqual(runtime.run_once()["job"], "succeeded")
        with self.factory() as session:
            self.assertEqual(
                session.get(TrainingJob, first.job_id).status, TrainingJobStatus.FAILED
            )
            self.assertEqual(
                session.get(TrainingJob, second.job_id).status, TrainingJobStatus.SUCCEEDED
            )

        systems = self.coordinator.enqueue(
            job("systems", created_at=NOW + timedelta(seconds=2), systems_only=True)
        )

        def invalid_publish(request, control):
            return {"model_status": "ACTIVE", "published": True}

        result = self.runtime(invalid_publish).run_once()
        self.assertEqual(result["job"], "failed")
        with self.factory() as session:
            self.assertEqual(
                session.get(TrainingJob, systems.job_id).status, TrainingJobStatus.FAILED
            )

    def test_running_cancellation_wins_handler_completion_race(self) -> None:
        queued = self.coordinator.enqueue(job("cancel-running"))

        def cancelled_during_handler(request, control):
            self.coordinator.cancel(job_id=request.job_id, now=self.clock())
            return {"model_status": "EVALUATED"}

        result = self.runtime(cancelled_during_handler).run_once()
        self.assertEqual(result["job"], "cancelled")
        with self.factory() as session:
            self.assertEqual(
                session.get(TrainingJob, queued.job_id).status,
                TrainingJobStatus.CANCELLED,
            )

    def test_latest_data_version_is_rejected_before_enqueue(self) -> None:
        mutable = job("mutable")
        mutable.data_version = "latest"
        with self.assertRaisesRegex(ValueError, "never latest"):
            self.coordinator.enqueue(mutable)

    def test_unconfigured_runtime_does_not_claim_or_fail_jobs(self) -> None:
        queued = self.coordinator.enqueue(job("handler-missing"))
        runtime = self.runtime(SuccessHandler(), handler_configured=False)
        result = runtime.run_once()
        self.assertEqual(result["job"], "training_handler_unconfigured")
        self.assertTrue(result["task_processing_enabled"])
        self.assertFalse(result["training_jobs_claimed"])
        readiness = runtime.readiness()
        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.status, "ready_degraded")
        self.assertTrue(readiness.task_processing_enabled)
        self.assertFalse(readiness.training_handler_configured)
        self.assertFalse(readiness.training_jobs_claimed)
        with self.factory() as session:
            self.assertEqual(
                session.get(TrainingJob, queued.job_id).status,
                TrainingJobStatus.QUEUED,
            )

    def test_run_once_cli_exit_codes_are_deterministic(self) -> None:
        runtime = self.runtime(SuccessHandler())
        with patch.object(worker_app, "build_runtime", return_value=runtime):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(worker_app.main(["run-once"]), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "activated_promotions": 0,
                "applied_batches": 0,
                "broker_degraded": False,
                "expired_promotions": 0,
                "job": "idle",
            },
        )

        disabled = self.runtime(SuccessHandler(), handler_configured=False)
        with patch.object(worker_app, "build_runtime", return_value=disabled):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(worker_app.main(["run-once"]), 2)
        self.assertIn('"job": "training_handler_unconfigured"', output.getvalue())

    def test_readiness_http_matrix_separates_worker_and_training_plugin(self) -> None:
        def ready_http(runtime: WorkerRuntime) -> tuple[int, dict[str, Any]]:
            server = ThreadingHTTPServer(("127.0.0.1", 0), worker_app.handler_class(runtime))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/ready"
                try:
                    with urllib.request.urlopen(url, timeout=2) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        degraded = self.runtime(SuccessHandler(), handler_configured=False)
        status, payload = ready_http(degraded)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready_degraded")
        self.assertTrue(payload["task_processing_enabled"])
        self.assertFalse(payload["training_handler_configured"])
        self.assertFalse(payload["training_jobs_claimed"])

        strict = self.runtime(
            SuccessHandler(), handler_configured=False, require_training_handler=True
        )
        status, payload = ready_http(strict)
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "not_ready")
        self.assertTrue(payload["require_training_handler"])

        healthy = self.runtime(SuccessHandler())
        status, payload = ready_http(healthy)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["training_jobs_claimed"])

        self.broker.fail = True
        status, payload = ready_http(healthy)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready_degraded")
        self.assertTrue(payload["redis_degraded"])

        missing_ops = WorkerRuntime(
            coordinator=self.coordinator,
            handler=SuccessHandler(),
            worker_id="worker-a",
            clock=self.clock,
        )
        status, payload = ready_http(missing_ops)
        self.assertEqual(status, 503)
        self.assertFalse(payload["scheduled_ops_runtime_configured"])

        self.broker.fail = False
        self.engine.dispose()
        status, payload = ready_http(healthy)
        self.assertEqual(status, 503)
        self.assertFalse(payload["database"])

    def test_training_handler_strict_mode_environment_parser(self) -> None:
        with patch.dict(os.environ, {"WORKER_REQUIRE_TRAINING_HANDLER": "true"}):
            self.assertTrue(
                worker_app.parse_bool_env("WORKER_REQUIRE_TRAINING_HANDLER", default=False)
            )
        with patch.dict(os.environ, {"WORKER_REQUIRE_TRAINING_HANDLER": "false"}):
            self.assertFalse(
                worker_app.parse_bool_env("WORKER_REQUIRE_TRAINING_HANDLER", default=True)
            )
        with patch.dict(os.environ, {"WORKER_REQUIRE_TRAINING_HANDLER": "invalid"}):
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                worker_app.parse_bool_env("WORKER_REQUIRE_TRAINING_HANDLER", default=False)


class ScheduledOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.clock = DateClock()
        with self.factory.begin() as session:
            self.operator = add_user(session, username="scheduled-operator", role=Role.OPERATOR)
            session.add(Item(id="item-1", title="Scheduled item"))

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_scheduled_apply_and_promotion_expiry_are_exactly_once(self) -> None:
        request = OperationBatchRequest.model_validate(
            {
                "batch_id": uuid.uuid4(),
                "operation_type": "promote",
                "targets": ["item-1"],
                "scope_type": "all",
                "scope_value": None,
                "starts_at_utc": NOW + timedelta(hours=1),
                "ends_at_utc": NOW + timedelta(hours=2),
                "priority": 10,
                "target_position": 0,
                "reason": "scheduled worker test",
                "semantics": "preflight_then_all_or_nothing_transaction",
            }
        )
        with self.factory.begin() as session:
            response = OperationService().create_batch(
                session, operator_id=self.operator.id, request=request, now=NOW
            )
        self.assertEqual(response.status, "scheduled")

        runner = ScheduledOperationsRunner(self.factory, clock=self.clock)
        self.assertEqual(runner.run_once()["applied_batches"], 0)
        self.clock.value = NOW + timedelta(hours=1)
        self.assertEqual(runner.run_once()["applied_batches"], 1)
        self.assertEqual(runner.run_once()["applied_batches"], 0)

        with self.factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count(Operation.id)).where(Operation.batch_id == request.batch_id)
                ),
                1,
            )
            rule = session.scalar(
                select(PromotionRule).where(PromotionRule.operation_batch_id == request.batch_id)
            )
            self.assertEqual(rule.status, PromotionStatus.ACTIVE)

        self.clock.value = NOW + timedelta(hours=2)
        self.assertEqual(runner.run_once()["expired_promotions"], 1)
        self.assertEqual(runner.run_once()["expired_promotions"], 0)
        with self.factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count(OperationBatch.batch_id)).where(
                        OperationBatch.batch_id == request.batch_id
                    )
                ),
                1,
            )
            rule = session.scalar(
                select(PromotionRule).where(PromotionRule.operation_batch_id == request.batch_id)
            )
            self.assertEqual(rule.status, PromotionStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()
