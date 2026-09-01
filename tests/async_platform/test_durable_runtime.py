from __future__ import annotations

import dataclasses
import unittest
import uuid
from datetime import timedelta

from sqlalchemy.dialects import postgresql

from apps.api.app.async_runtime import IdempotencyConflict, JobSpec, JobState, LeaseLost
from apps.api.app.async_runtime.domain import safe_error
from apps.api.app.async_runtime.service import DurableJobService, OutboxHintDispatcher
from apps.api.app.async_runtime.tables import AsyncJobAttemptRow, AsyncOutboxRow
from tests.async_platform._support import NOW, RecordingHintSink, runtime


class DurableRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory, self.repository, self.jobs = runtime()

    def spec(self, **changes) -> JobSpec:
        values = {
            "idempotency_key": "digest-2026-09-01",
            "task_name": "digest",
            "payload": {"segment": "all"},
            "due_at": NOW,
            "max_attempts": 3,
            "job_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        }
        values.update(changes)
        return JobSpec(**values)

    def test_enqueue_is_idempotent_and_conflicting_payload_is_rejected(self) -> None:
        first, created = self.jobs.enqueue(self.spec(), now=NOW)
        second, duplicate_created = self.jobs.enqueue(self.spec(), now=NOW)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first, second)
        with self.assertRaises(IdempotencyConflict):
            self.jobs.enqueue(self.spec(payload={"segment": "vip"}), now=NOW)

        with self.factory() as session:
            self.assertEqual(session.query(AsyncOutboxRow).count(), 1)

        with self.assertRaises(ValueError):
            self.spec(payload={"invalid": float("nan")})

    def test_not_due_and_cancel_are_database_authoritative(self) -> None:
        job, _ = self.jobs.enqueue(self.spec(due_at=NOW + timedelta(minutes=5)), now=NOW)
        self.assertIsNone(self.jobs.claim_next(worker_id="w1", now=NOW))
        cancelled = self.repository.cancel(job.job_id, now=NOW)
        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertFalse(cancelled.duplicate)
        self.assertTrue(self.repository.cancel(job.job_id, now=NOW).duplicate)
        self.assertIsNone(self.jobs.claim_next(worker_id="w1", now=NOW + timedelta(hours=1)))

    def test_lease_fence_heartbeat_retry_and_duplicate_completion(self) -> None:
        self.jobs.enqueue(self.spec(), now=NOW)
        claim = self.jobs.claim_next(worker_id="w1", now=NOW)
        self.assertIsNotNone(claim)
        assert claim is not None
        extended = self.jobs.heartbeat(claim, now=NOW + timedelta(seconds=2))
        self.assertEqual(extended, NOW + timedelta(seconds=12))

        forged = dataclasses.replace(claim, fence_token=uuid.uuid4())
        with self.assertRaises(LeaseLost):
            self.jobs.heartbeat(forged, now=NOW + timedelta(seconds=3))

        failed = self.jobs.fail(
            claim,
            error=ConnectionError("temporary"),
            retryable=True,
            retry_delay_seconds=5,
            now=NOW + timedelta(seconds=3),
        )
        self.assertEqual(failed.state, JobState.QUEUED)
        self.assertIsNone(self.jobs.claim_next(worker_id="w2", now=NOW + timedelta(seconds=7)))
        second = self.jobs.claim_next(worker_id="w2", now=NOW + timedelta(seconds=8))
        self.assertIsNotNone(second)
        assert second is not None
        result = {"artifact": "sha256:" + "a" * 64}
        completion = self.jobs.succeed(second, result=result, now=NOW + timedelta(seconds=9))
        self.assertFalse(completion.duplicate)
        self.assertTrue(
            self.jobs.succeed(second, result=result, now=NOW + timedelta(seconds=9)).duplicate
        )
        with self.assertRaises(IdempotencyConflict):
            self.jobs.succeed(
                second, result={"artifact": "different"}, now=NOW + timedelta(seconds=9)
            )

    def test_restart_recovers_expired_lease_without_redis(self) -> None:
        self.jobs.enqueue(self.spec(), now=NOW)
        abandoned = self.jobs.claim_next(worker_id="dead-worker", now=NOW)
        self.assertIsNotNone(abandoned)
        recovered = self.jobs.claim_next(worker_id="replacement", now=NOW + timedelta(seconds=11))
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.attempt, 2)
        with self.factory() as session:
            attempts = session.query(AsyncJobAttemptRow).order_by(AsyncJobAttemptRow.attempt).all()
            self.assertEqual([row.status for row in attempts], ["lease_expired", "running"])

    def test_exhausted_lease_emits_terminal_outbox_and_errors_are_redacted(self) -> None:
        job, _ = self.jobs.enqueue(self.spec(max_attempts=1), now=NOW)
        self.assertIsNotNone(self.jobs.claim_next(worker_id="dead-worker", now=NOW))
        self.assertIsNone(
            self.jobs.claim_next(worker_id="replacement", now=NOW + timedelta(seconds=11))
        )
        self.assertEqual(self.repository.get(job.job_id).state, JobState.FAILED)
        with self.factory() as session:
            topics = [row.topic for row in session.query(AsyncOutboxRow).all()]
        self.assertIn("async.job.failed", topics)
        message = safe_error(
            RuntimeError(
                "password=hunter2 Authorization:Bearer abc token=qwerty "
                "postgresql://admin:dbpass@db/service"
            )
        )
        for secret in ("hunter2", "abc", "qwerty", "dbpass"):
            self.assertNotIn(secret, message)

    def test_postgres_claim_contract_uses_skip_locked(self) -> None:
        sql = str(
            self.repository.queued_claim_query(now=NOW, task_names={"digest"}).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)

    def test_cancelling_running_job_fences_the_old_worker(self) -> None:
        job, _ = self.jobs.enqueue(self.spec(), now=NOW)
        claim = self.jobs.claim_next(worker_id="w1", now=NOW)
        assert claim is not None
        self.repository.cancel(job.job_id, now=NOW + timedelta(seconds=1))
        with self.assertRaises(LeaseLost):
            self.jobs.succeed(claim, result={"late": True}, now=NOW + timedelta(seconds=2))

    def test_redis_hint_outage_does_not_lose_work_and_outbox_retries(self) -> None:
        failing_hint = RecordingHintSink(failures=1)
        jobs = DurableJobService(self.repository, hint_sink=failing_hint, lease_seconds=10)
        job, created = jobs.enqueue(self.spec(), now=NOW)
        self.assertTrue(created)
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertIsNotNone(jobs.claim_next(worker_id="poller", now=NOW))

        sink = RecordingHintSink(failures=1)
        dispatcher = OutboxHintDispatcher(
            self.repository, sink, lease_seconds=5, retry_delay_seconds=2
        )
        first = dispatcher.run_once(now=NOW)
        self.assertEqual(first, {"claimed": 1, "published": 0, "failed": 1})
        self.assertEqual(dispatcher.run_once(now=NOW + timedelta(seconds=1))["claimed"], 0)
        second = dispatcher.run_once(now=NOW + timedelta(seconds=2))
        self.assertEqual(second, {"claimed": 1, "published": 1, "failed": 0})

    def test_outbox_publish_crash_can_duplicate_only_reconstructable_hint(self) -> None:
        self.jobs.enqueue(self.spec(), now=NOW)
        first = self.repository.claim_outbox(now=NOW, lease_seconds=5, limit=1)[0]
        delivered = [(first.topic, first.payload)]  # publish happened; process died before ACK
        self.assertEqual(
            self.repository.claim_outbox(now=NOW + timedelta(seconds=4), lease_seconds=5), []
        )
        replay = self.repository.claim_outbox(
            now=NOW + timedelta(seconds=5), lease_seconds=5, limit=1
        )[0]
        delivered.append((replay.topic, replay.payload))
        self.repository.ack_outbox(replay, now=NOW + timedelta(seconds=5))
        self.assertEqual(delivered[0], delivered[1])
        self.assertFalse(self.repository.ack_outbox(replay, now=NOW + timedelta(seconds=6)))


if __name__ == "__main__":
    unittest.main()
