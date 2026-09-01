from __future__ import annotations

import unittest
import uuid
from copy import deepcopy
from datetime import timedelta

from apps.api.app.async_runtime import JobSpec, JobState, LeaseLost
from apps.api.app.operation_jobs import (
    ExpectedStateConflict,
    OperationBatchResult,
    OperationJobService,
    OperationJobSpec,
    OperationKind,
    OperationTaskHandler,
    TargetExpectation,
)
from apps.worker.async_tasks import RetryableTaskError, RunOnceWorker, SimulatedWorkerCrash
from tests.async_platform._support import NOW, MutableClock, runtime


class AtomicTargetExecutor:
    def __init__(self) -> None:
        self.targets = {
            "item-a": {"state_version": 1, "online": True},
            "item-b": {"state_version": 4, "online": True},
        }
        self.receipts: dict[uuid.UUID, OperationBatchResult] = {}
        self.retryable_failures = 0
        self.crash_after_commit_once = False
        self.after_commit = None

    def apply_all(self, *, operation_id, kind, targets, payload, now):
        if operation_id in self.receipts:
            original = self.receipts[operation_id]
            return OperationBatchResult(
                operation_id=original.operation_id,
                applied_targets=original.applied_targets,
                state_versions=original.state_versions,
                duplicate=True,
            )
        if self.retryable_failures:
            self.retryable_failures -= 1
            raise RetryableTaskError("transient database pressure")

        staged = deepcopy(self.targets)
        for expected in sorted(targets, key=lambda target: target.target_id):
            target = staged.get(expected.target_id)
            if target is None or target["state_version"] != expected.state_version:
                raise ExpectedStateConflict(expected.target_id)
        for expected in sorted(targets, key=lambda target: target.target_id):
            target = staged[expected.target_id]
            if kind == OperationKind.OFFLINE:
                target["online"] = False
            elif kind == OperationKind.RESTORE:
                target["online"] = True
            target["state_version"] += 1
        result = OperationBatchResult(
            operation_id=operation_id,
            applied_targets=tuple(sorted(target.target_id for target in targets)),
            state_versions={
                target.target_id: int(staged[target.target_id]["state_version"])
                for target in targets
            },
        )
        # This assignment and receipt represent one transaction in the integration contract.
        self.targets = staged
        self.receipts[operation_id] = result
        if self.after_commit is not None:
            self.after_commit()
        if self.crash_after_commit_once:
            self.crash_after_commit_once = False
            raise SimulatedWorkerCrash("process exited after operation commit")
        return result


class OperationJobTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.repository, self.jobs = runtime()
        self.operations = OperationJobService(self.jobs, self.repository)
        self.executor = AtomicTargetExecutor()
        self.clock = MutableClock()
        self.worker = RunOnceWorker(
            self.jobs,
            [OperationTaskHandler(self.executor)],
            worker_id="ops-worker",
            clock=self.clock,
            retry_delay_seconds=3,
        )

    def run_at(self, now):
        self.clock.value = now
        return self.worker.run_once()

    def spec(self, **changes) -> OperationJobSpec:
        values = {
            "operation_id": uuid.UUID("33333333-3333-4333-8333-333333333333"),
            "idempotency_key": "offline-a-b",
            "kind": OperationKind.OFFLINE,
            "targets": (TargetExpectation("item-a", 1), TargetExpectation("item-b", 4)),
            "due_at": NOW,
            "payload": {"reason": "policy"},
            "max_attempts": 3,
        }
        values.update(changes)
        return OperationJobSpec(**values)

    def test_not_due_then_applies_every_target_exactly_once(self) -> None:
        job, created = self.operations.submit(self.spec(due_at=NOW + timedelta(minutes=1)), now=NOW)
        self.assertTrue(created)
        self.assertEqual(self.run_at(NOW)["state"], "idle")
        result = self.run_at(NOW + timedelta(minutes=1))
        self.assertEqual(result["state"], "succeeded")
        self.assertFalse(self.executor.targets["item-a"]["online"])
        self.assertFalse(self.executor.targets["item-b"]["online"])
        versions = {key: value["state_version"] for key, value in self.executor.targets.items()}
        self.assertEqual(versions, {"item-a": 2, "item-b": 5})
        self.assertEqual(self.run_at(NOW + timedelta(minutes=2))["state"], "idle")
        self.assertEqual(self.repository.get(job.job_id).state, JobState.SUCCEEDED)

    def test_expected_version_conflict_is_all_or_none(self) -> None:
        before = deepcopy(self.executor.targets)
        self.operations.submit(
            self.spec(targets=(TargetExpectation("item-a", 1), TargetExpectation("item-b", 999))),
            now=NOW,
        )
        result = self.run_at(NOW)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.executor.targets, before)

    def test_retryable_failure_waits_and_then_succeeds(self) -> None:
        self.executor.retryable_failures = 1
        self.operations.submit(self.spec(), now=NOW)
        self.assertEqual(self.run_at(NOW)["state"], "queued")
        self.assertEqual(self.run_at(NOW + timedelta(seconds=2))["state"], "idle")
        self.assertEqual(self.run_at(NOW + timedelta(seconds=3))["state"], "succeeded")
        self.assertEqual(len(self.executor.receipts), 1)

    def test_explicit_retry_after_state_is_corrected(self) -> None:
        spec = self.spec(targets=(TargetExpectation("item-a", 1), TargetExpectation("item-b", 999)))
        job, _ = self.operations.submit(spec, now=NOW)
        self.assertEqual(self.run_at(NOW)["state"], "failed")
        self.executor.targets["item-b"]["state_version"] = 999
        retried = self.operations.retry(job.job_id, due_at=NOW + timedelta(seconds=1), now=NOW)
        self.assertEqual(retried.state, JobState.QUEUED)
        self.assertEqual(self.run_at(NOW + timedelta(seconds=1))["state"], "succeeded")

    def test_crash_after_commit_recovers_via_lease_and_operation_receipt(self) -> None:
        self.executor.crash_after_commit_once = True
        self.operations.submit(self.spec(), now=NOW)
        with self.assertRaises(SimulatedWorkerCrash):
            self.run_at(NOW)
        versions_after_commit = {
            key: value["state_version"] for key, value in self.executor.targets.items()
        }
        self.assertEqual(versions_after_commit, {"item-a": 2, "item-b": 5})
        self.assertEqual(self.run_at(NOW + timedelta(seconds=9))["state"], "idle")
        recovered = self.run_at(NOW + timedelta(seconds=11))
        self.assertEqual(recovered["state"], "succeeded")
        self.assertEqual(
            {key: value["state_version"] for key, value in self.executor.targets.items()},
            versions_after_commit,
        )

    def test_handler_elapsed_time_cannot_bypass_expired_lease(self) -> None:
        self.executor.after_commit = lambda: setattr(
            self.clock, "value", NOW + timedelta(seconds=11)
        )
        self.operations.submit(self.spec(), now=NOW)
        with self.assertRaises(LeaseLost):
            self.run_at(NOW)
        versions_after_commit = {
            key: value["state_version"] for key, value in self.executor.targets.items()
        }
        self.executor.after_commit = None
        self.assertEqual(self.run_at(NOW + timedelta(seconds=11))["state"], "succeeded")
        self.assertEqual(
            {key: value["state_version"] for key, value in self.executor.targets.items()},
            versions_after_commit,
        )

    def test_cancelled_scheduled_operation_never_executes(self) -> None:
        job, _ = self.operations.submit(self.spec(due_at=NOW + timedelta(minutes=1)), now=NOW)
        cancelled = self.operations.cancel(job.job_id, now=NOW + timedelta(seconds=1))
        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertEqual(self.run_at(NOW + timedelta(minutes=2))["state"], "idle")
        self.assertEqual(self.executor.receipts, {})

    def test_state_versions_reject_boolean_and_string_coercion(self) -> None:
        with self.assertRaises(ValueError):
            TargetExpectation("item-a", True)
        with self.assertRaises(ValueError):
            OperationBatchResult(
                operation_id=uuid.uuid4(),
                applied_targets=("item-a",),
                state_versions={"item-a": True},
            )

        malicious = self.jobs.enqueue(
            JobSpec(
                idempotency_key="malformed-operation",
                task_name="operation_batch",
                payload={
                    "schema_version": 1,
                    "operation_id": "44444444-4444-4444-8444-444444444444",
                    "kind": "offline",
                    "targets": [{"target_id": "item-a", "state_version": "1"}],
                    "operation_payload": {},
                },
                due_at=NOW,
                job_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
            ),
            now=NOW,
        )[0]
        self.assertEqual(self.run_at(NOW)["state"], "failed")
        self.assertEqual(self.repository.get(malicious.job_id).state, JobState.FAILED)


if __name__ == "__main__":
    unittest.main()
