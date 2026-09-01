from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from apps.api.app.db.models import TrainingJob

from .contracts import (
    CancellationRequested,
    PermanentTrainingError,
    RetryableTrainingError,
    TrainingControl,
    TrainingHandler,
)
from .jobs import JobCoordinator
from .operations import ScheduledOperationsRunner


def utc_now() -> datetime:
    return datetime.now(UTC)


def structured_log(event: str, **fields: Any) -> None:
    print(
        json.dumps(
            {"service": "worker", "event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
        flush=True,
    )


@dataclass(frozen=True)
class Readiness:
    database: bool
    redis: bool
    redis_degraded: bool
    task_processing_enabled: bool
    scheduled_ops_runtime_configured: bool
    training_handler_configured: bool
    training_jobs_claimed: bool
    require_training_handler: bool

    @property
    def ready(self) -> bool:
        return (
            self.database
            and self.task_processing_enabled
            and self.scheduled_ops_runtime_configured
            and (self.training_handler_configured or not self.require_training_handler)
        )

    @property
    def status(self) -> str:
        if not self.ready:
            return "not_ready"
        if self.redis_degraded or not self.training_handler_configured:
            return "ready_degraded"
        return "ready"

    def as_dict(self) -> dict[str, bool | str]:
        return {
            "status": self.status,
            "database": self.database,
            "redis": self.redis,
            "redis_degraded": self.redis_degraded,
            "task_processing_enabled": self.task_processing_enabled,
            "scheduled_ops_runtime_configured": self.scheduled_ops_runtime_configured,
            "training_handler_configured": self.training_handler_configured,
            "training_jobs_claimed": self.training_jobs_claimed,
            "require_training_handler": self.require_training_handler,
        }


class WorkerRuntime:
    def __init__(
        self,
        *,
        coordinator: JobCoordinator,
        handler: TrainingHandler,
        worker_id: str,
        clock: Callable[[], datetime] = utc_now,
        scheduled_operations: ScheduledOperationsRunner | None = None,
        handler_configured: bool = True,
        require_training_handler: bool = False,
    ) -> None:
        self.coordinator = coordinator
        self.handler = handler
        self.worker_id = worker_id
        self.clock = clock
        self.scheduled_operations = scheduled_operations
        self.handler_configured = handler_configured
        self.require_training_handler = require_training_handler
        self.processed_count = 0
        self.failed_count = 0

    def run_once(self) -> dict[str, Any]:
        operations = (
            self.scheduled_operations.run_once()
            if self.scheduled_operations is not None
            else {
                "applied_batches": 0,
                "activated_promotions": 0,
                "expired_promotions": 0,
            }
        )
        if not self.handler_configured:
            return {
                "job": "training_handler_unconfigured",
                "task_processing_enabled": self.scheduled_operations is not None,
                "training_handler_configured": False,
                "training_jobs_claimed": False,
                **operations,
            }
        broker_degraded = False
        if self.coordinator.broker is not None:
            try:
                self.coordinator.broker.receive(timeout_seconds=0)
            except Exception as exc:
                broker_degraded = True
                structured_log("broker_receive_degraded", error=type(exc).__name__)

        claim = self.coordinator.claim_next(worker_id=self.worker_id, now=self.clock())
        if claim is None:
            return {"job": "idle", "broker_degraded": broker_degraded, **operations}

        request = claim.request
        structured_log(
            "job_claimed",
            job_id=request.job_id,
            attempt_id=request.attempt_id,
            attempt=request.attempt,
            data_version=request.data_version,
            purpose=request.purpose.value,
        )

        def heartbeat() -> None:
            self.coordinator.heartbeat(
                job_id=request.job_id,
                attempt_id=request.attempt_id,
                worker_id=self.worker_id,
                now=self.clock(),
            )

        def cancellation_requested() -> bool:
            return self.coordinator.cancellation_requested(job_id=request.job_id)

        try:
            if cancellation_requested():
                raise CancellationRequested("job was cancelled before handler start")
            result = self.handler(
                request,
                TrainingControl(
                    heartbeat=heartbeat,
                    cancellation_requested=cancellation_requested,
                ),
            )
            self.coordinator.succeed(
                claim=claim,
                worker_id=self.worker_id,
                result=result,
                now=self.clock(),
            )
        except CancellationRequested:
            self.coordinator.cancel(job_id=request.job_id, now=self.clock())
            structured_log("job_cancelled", job_id=request.job_id, attempt=request.attempt)
            return {
                "job": "cancelled",
                "job_id": str(request.job_id),
                "broker_degraded": broker_degraded,
                **operations,
            }
        except Exception as exc:
            if self.coordinator.cancellation_requested(job_id=request.job_id):
                self.coordinator.cancel(job_id=request.job_id, now=self.clock())
                structured_log("job_cancelled", job_id=request.job_id, attempt=request.attempt)
                return {
                    "job": "cancelled",
                    "job_id": str(request.job_id),
                    "broker_degraded": broker_degraded,
                    **operations,
                }
            retryable = isinstance(exc, RetryableTrainingError) and not isinstance(
                exc, PermanentTrainingError
            )
            status = self.coordinator.fail(
                claim=claim,
                worker_id=self.worker_id,
                error=exc,
                retryable=retryable,
                now=self.clock(),
            )
            self.failed_count += 1
            structured_log(
                "job_failed",
                job_id=request.job_id,
                attempt=request.attempt,
                retryable=retryable,
                status=status.value,
                error_type=type(exc).__name__,
            )
            return {
                "job": status.value,
                "job_id": str(request.job_id),
                "attempt": request.attempt,
                "broker_degraded": broker_degraded,
                **operations,
            }
        self.processed_count += 1
        structured_log("job_succeeded", job_id=request.job_id, attempt=request.attempt)
        return {
            "job": "succeeded",
            "job_id": str(request.job_id),
            "attempt": request.attempt,
            "broker_degraded": broker_degraded,
            **operations,
        }

    def readiness(self) -> Readiness:
        database = False
        try:
            with self.coordinator.session_factory() as session:
                session.execute(select(TrainingJob.job_id).limit(1))
            database = True
        except Exception:
            database = False

        redis_ready = False
        redis_degraded = self.coordinator.broker is None
        if self.coordinator.broker is not None:
            try:
                redis_ready = bool(self.coordinator.broker.ping())
            except Exception:
                redis_ready = False
            redis_degraded = not redis_ready
        return Readiness(
            database=database,
            redis=redis_ready,
            redis_degraded=redis_degraded,
            task_processing_enabled=self.scheduled_operations is not None,
            scheduled_ops_runtime_configured=self.scheduled_operations is not None,
            training_handler_configured=self.handler_configured,
            training_jobs_claimed=self.handler_configured,
            require_training_handler=self.require_training_handler,
        )
