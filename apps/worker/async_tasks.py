from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from apps.api.app.async_runtime.domain import DurableClaim, require_aware
from apps.api.app.async_runtime.service import DurableJobService, OutboxHintDispatcher


class TaskHandler(Protocol):
    task_name: str

    def handle(self, claim: DurableClaim, *, now: datetime) -> dict[str, object]: ...


class RetryableTaskError(RuntimeError):
    pass


class SimulatedWorkerCrash(BaseException):
    """Test-only crash signal; bypasses normal Exception failure handling."""


class RunOnceWorker:
    """A deterministic worker pass that always reconciles authoritative DB rows."""

    def __init__(
        self,
        jobs: DurableJobService,
        handlers: list[TaskHandler],
        *,
        worker_id: str,
        clock: Callable[[], datetime],
        retry_delay_seconds: int = 5,
    ) -> None:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be nonnegative")
        self.jobs = jobs
        self.handlers = {handler.task_name: handler for handler in handlers}
        if len(self.handlers) != len(handlers):
            raise ValueError("task handler names must be unique")
        self.worker_id = worker_id
        self.clock = clock
        self.retry_delay_seconds = retry_delay_seconds

    def run_once(self) -> dict[str, object]:
        event_time = require_aware(self.clock(), field="clock")
        claim = self.jobs.claim_next(
            worker_id=self.worker_id,
            now=event_time,
            task_names=set(self.handlers),
        )
        if claim is None:
            return {"claimed": 0, "state": "idle"}
        handler = self.handlers[claim.job.task_name]
        try:
            result = handler.handle(claim, now=event_time)
        except RetryableTaskError as exc:
            completed_at = require_aware(self.clock(), field="clock")
            completion = self.jobs.fail(
                claim,
                error=exc,
                retryable=True,
                retry_delay_seconds=self.retry_delay_seconds,
                now=completed_at,
            )
            return {"claimed": 1, "job_id": str(claim.job.job_id), "state": completion.state.value}
        except Exception as exc:
            completed_at = require_aware(self.clock(), field="clock")
            completion = self.jobs.fail(
                claim,
                error=exc,
                retryable=False,
                retry_delay_seconds=0,
                now=completed_at,
            )
            return {"claimed": 1, "job_id": str(claim.job.job_id), "state": completion.state.value}
        completed_at = require_aware(self.clock(), field="clock")
        completion = self.jobs.succeed(claim, result=result, now=completed_at)
        return {
            "claimed": 1,
            "job_id": str(claim.job.job_id),
            "state": completion.state.value,
            "duplicate_completion": completion.duplicate,
        }


class RunOnceScheduler:
    """Dispatch hints and execute due work without relying on Redis delivery."""

    def __init__(
        self,
        worker: RunOnceWorker,
        *,
        outbox: OutboxHintDispatcher | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        self.worker = worker
        self.outbox = outbox
        self.clock = clock

    def run_once(self) -> dict[str, object]:
        now = require_aware(self.clock(), field="clock")
        outbox_result = self.outbox.run_once(now=now) if self.outbox is not None else {"claimed": 0}
        worker_result = self.worker.run_once()
        return {"outbox": outbox_result, "worker": worker_result}
