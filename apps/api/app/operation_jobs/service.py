from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from apps.api.app.async_runtime.domain import (
    Completion,
    DurableClaim,
    DurableJob,
    JobSpec,
    require_aware,
)
from apps.api.app.async_runtime.repository import SqlAlchemyAsyncRepository
from apps.api.app.async_runtime.service import DurableJobService

from .domain import (
    OperationBatchResult,
    OperationJobSpec,
    OperationKind,
    TargetExpectation,
)

OPERATION_TASK_NAME = "operation_batch"


class AtomicOperationExecutor(Protocol):
    """Integration contract for one PostgreSQL transaction.

    The implementation must lock all targets in stable order, validate every expected
    state version, apply all mutations, and persist an operation-id receipt before
    commit. Any failure must roll back every target. A duplicate operation_id returns
    the original result without applying again.
    """

    def apply_all(
        self,
        *,
        operation_id: uuid.UUID,
        kind: OperationKind,
        targets: tuple[TargetExpectation, ...],
        payload: dict[str, Any],
        now: datetime,
    ) -> OperationBatchResult: ...


class OperationJobService:
    def __init__(self, jobs: DurableJobService, repository: SqlAlchemyAsyncRepository) -> None:
        self.jobs = jobs
        self.repository = repository

    def submit(self, spec: OperationJobSpec, *, now: datetime) -> tuple[DurableJob, bool]:
        payload = {
            "schema_version": 1,
            "operation_id": str(spec.operation_id),
            "kind": spec.kind.value,
            "targets": [
                {"target_id": target.target_id, "state_version": target.state_version}
                for target in spec.targets
            ],
            "operation_payload": spec.payload,
        }
        return self.jobs.enqueue(
            JobSpec(
                job_id=spec.operation_id,
                idempotency_key=f"operation:{spec.idempotency_key}",
                task_name=OPERATION_TASK_NAME,
                payload=payload,
                due_at=spec.due_at,
                max_attempts=spec.max_attempts,
            ),
            now=now,
        )

    def cancel(self, operation_id: uuid.UUID, *, now: datetime) -> Completion:
        return self.repository.cancel(operation_id, now=now)

    def retry(self, operation_id: uuid.UUID, *, due_at: datetime, now: datetime) -> DurableJob:
        return self.repository.retry(operation_id, due_at=due_at, now=now)


class OperationTaskHandler:
    task_name = OPERATION_TASK_NAME

    def __init__(self, executor: AtomicOperationExecutor) -> None:
        self.executor = executor

    def handle(self, claim: DurableClaim, *, now: datetime) -> dict[str, object]:
        event_time = require_aware(now, field="now")
        if claim.job.task_name != self.task_name:
            raise ValueError("operation handler received the wrong task")
        payload = claim.job.payload
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported operation payload schema")
        operation_id = uuid.UUID(str(payload["operation_id"]))
        if operation_id != claim.job.job_id:
            raise ValueError("operation_id must equal durable job_id")
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, list):
            raise ValueError("operation targets must be an array")
        targets_list: list[TargetExpectation] = []
        for target in raw_targets:
            if not isinstance(target, dict):
                raise ValueError("operation target must be an object")
            target_id = target.get("target_id")
            state_version = target.get("state_version")
            if not isinstance(target_id, str):
                raise ValueError("operation target_id must be a string")
            if isinstance(state_version, bool) or not isinstance(state_version, int):
                raise ValueError("operation state_version must be an integer")
            targets_list.append(TargetExpectation(target_id=target_id, state_version=state_version))
        targets = tuple(targets_list)
        if not 1 <= len(targets) <= 100 or len({target.target_id for target in targets}) != len(
            targets
        ):
            raise ValueError("operation payload has invalid target cardinality")
        operation_payload = payload.get("operation_payload")
        if not isinstance(operation_payload, dict):
            raise ValueError("operation_payload must be an object")
        result = self.executor.apply_all(
            operation_id=operation_id,
            kind=OperationKind(str(payload["kind"])),
            targets=targets,
            payload=operation_payload,
            now=event_time,
        )
        expected_ids = {target.target_id for target in targets}
        if set(result.applied_targets) != expected_ids:
            raise ValueError("executor violated all-target result contract")
        return {
            "operation_id": str(result.operation_id),
            "applied_targets": list(result.applied_targets),
            "state_versions": result.state_versions,
            "duplicate": result.duplicate,
        }
