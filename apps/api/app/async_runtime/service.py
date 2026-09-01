from __future__ import annotations

from datetime import datetime

from .domain import Completion, DurableClaim, DurableJob, HintSink, JobSpec, require_aware
from .repository import SqlAlchemyAsyncRepository


class DurableJobService:
    def __init__(
        self,
        repository: SqlAlchemyAsyncRepository,
        *,
        hint_sink: HintSink | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.repository = repository
        self.hint_sink = hint_sink
        self.lease_seconds = lease_seconds

    def enqueue(self, spec: JobSpec, *, now: datetime) -> tuple[DurableJob, bool]:
        job, created = self.repository.enqueue(spec, now=now)
        if created:
            self._best_effort_hint(
                "async.job.queued", {"job_id": str(job.job_id), "task_name": job.task_name}
            )
        return job, created

    def claim_next(
        self, *, worker_id: str, now: datetime, task_names: set[str] | None = None
    ) -> DurableClaim | None:
        return self.repository.claim_next(
            worker_id=worker_id,
            now=now,
            lease_seconds=self.lease_seconds,
            task_names=task_names,
        )

    def heartbeat(self, claim: DurableClaim, *, now: datetime) -> datetime:
        return self.repository.heartbeat(claim, now=now, lease_seconds=self.lease_seconds)

    def succeed(
        self, claim: DurableClaim, *, result: dict[str, object], now: datetime
    ) -> Completion:
        return self.repository.succeed(claim, result=result, now=now)

    def fail(
        self,
        claim: DurableClaim,
        *,
        error: BaseException,
        retryable: bool,
        retry_delay_seconds: int,
        now: datetime,
    ) -> Completion:
        completion = self.repository.fail(
            claim,
            error=error,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
            now=now,
        )
        if completion.state.value == "queued":
            self._best_effort_hint(
                "async.job.queued",
                {"job_id": str(claim.job.job_id), "task_name": claim.job.task_name},
            )
        return completion

    def _best_effort_hint(self, topic: str, payload: dict[str, object]) -> None:
        if self.hint_sink is None:
            return
        try:
            self.hint_sink.notify(topic, payload)
        except Exception:
            # The durable row and outbox are already committed. Polling reconstructs work.
            return


class OutboxHintDispatcher:
    """Run-once publisher for lossy Redis hints backed by a durable outbox."""

    def __init__(
        self,
        repository: SqlAlchemyAsyncRepository,
        sink: HintSink,
        *,
        lease_seconds: int = 30,
        retry_delay_seconds: int = 5,
    ) -> None:
        if lease_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("invalid outbox dispatcher timing")
        self.repository = repository
        self.sink = sink
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds

    def run_once(self, *, now: datetime, limit: int = 100) -> dict[str, int]:
        event_time = require_aware(now, field="now")
        claims = self.repository.claim_outbox(
            now=event_time, lease_seconds=self.lease_seconds, limit=limit
        )
        published = 0
        failed = 0
        for claim in claims:
            try:
                self.sink.notify(claim.topic, claim.payload)
            except Exception as exc:
                self.repository.nack_outbox(
                    claim,
                    error=exc,
                    now=event_time,
                    retry_delay_seconds=self.retry_delay_seconds,
                )
                failed += 1
            else:
                self.repository.ack_outbox(claim, now=event_time)
                published += 1
        return {"claimed": len(claims), "published": published, "failed": failed}
