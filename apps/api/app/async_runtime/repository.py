from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.db.base import ensure_utc

from .domain import (
    AttemptState,
    Completion,
    DurableClaim,
    DurableJob,
    IdempotencyConflict,
    JobSpec,
    JobState,
    LeaseLost,
    OutboxClaim,
    OutboxState,
    payload_fingerprint,
    require_aware,
    safe_error,
)
from .tables import AsyncJobAttemptRow, AsyncJobRow, AsyncOutboxRow


class SqlAlchemyAsyncRepository:
    """PostgreSQL-authoritative durable job/outbox repository.

    Production expects PostgreSQL, where `FOR UPDATE SKIP LOCKED` provides concurrent
    claim isolation. SQLite is supported only for deterministic contract tests; it does
    not prove PostgreSQL lock behavior.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def enqueue(self, spec: JobSpec, *, now: datetime) -> tuple[DurableJob, bool]:
        event_time = require_aware(now, field="now")
        due_at = spec.due_at.astimezone(UTC)
        job_id = spec.job_id or uuid.uuid4()
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(AsyncJobRow).where(AsyncJobRow.idempotency_key == spec.idempotency_key)
            )
            if existing is not None:
                return self._same_or_conflict(existing, spec), False

            job = AsyncJobRow(
                job_id=job_id,
                idempotency_key=spec.idempotency_key,
                task_name=spec.task_name,
                payload=spec.payload,
                payload_fingerprint=spec.fingerprint,
                status=JobState.QUEUED.value,
                due_at=due_at,
                max_attempts=spec.max_attempts,
                attempt_count=0,
                created_at=event_time,
                updated_at=event_time,
            )
            outbox = AsyncOutboxRow(
                idempotency_key=f"job:{job_id}:queued:0",
                topic="async.job.queued",
                payload={"job_id": str(job_id), "task_name": spec.task_name},
                status=OutboxState.PENDING.value,
                available_at=event_time,
                created_at=event_time,
            )
            try:
                with session.begin_nested():
                    session.add_all([job, outbox])
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(AsyncJobRow).where(AsyncJobRow.idempotency_key == spec.idempotency_key)
                )
                if existing is None:
                    raise
                return self._same_or_conflict(existing, spec), False
            return self._job_view(job), True

    def get(self, job_id: uuid.UUID) -> DurableJob | None:
        with self.session_factory() as session:
            row = session.get(AsyncJobRow, job_id)
            return self._job_view(row) if row is not None else None

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        task_names: set[str] | None = None,
    ) -> DurableClaim | None:
        if not worker_id or len(worker_id) > 255:
            raise ValueError("worker_id must contain 1..255 characters")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            self._recover_expired(session, now=event_time)
            session.flush()
            if task_names is not None and not task_names:
                return None
            job = session.scalar(self.queued_claim_query(now=event_time, task_names=task_names))
            if job is None:
                return None
            attempt_number = job.attempt_count + 1
            if attempt_number > job.max_attempts:
                self._mark_exhausted(job, now=event_time)
                self._add_state_outbox(session, job=job, state=JobState.FAILED, now=event_time)
                return None
            attempt_id = uuid.uuid4()
            fence_token = uuid.uuid4()
            lease_expires_at = event_time + timedelta(seconds=lease_seconds)
            attempt = AsyncJobAttemptRow(
                attempt_id=attempt_id,
                job_id=job.job_id,
                attempt=attempt_number,
                worker_id=worker_id,
                fence_token=fence_token,
                status=AttemptState.RUNNING.value,
                lease_expires_at=lease_expires_at,
                heartbeat_at=event_time,
                started_at=event_time,
            )
            session.add(attempt)
            job.status = JobState.RUNNING.value
            job.attempt_count = attempt_number
            job.last_error = None
            job.updated_at = event_time
            session.flush()
            return DurableClaim(
                job=self._job_view(job),
                attempt_id=attempt_id,
                attempt=attempt_number,
                worker_id=worker_id,
                fence_token=fence_token,
                lease_expires_at=lease_expires_at,
            )

    def heartbeat(self, claim: DurableClaim, *, now: datetime, lease_seconds: int) -> datetime:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            job, attempt = self._owned_attempt(session, claim=claim, now=event_time)
            lease_expires_at = event_time + timedelta(seconds=lease_seconds)
            attempt.heartbeat_at = event_time
            attempt.lease_expires_at = lease_expires_at
            job.updated_at = event_time
            return lease_expires_at

    def succeed(
        self,
        claim: DurableClaim,
        *,
        result: dict[str, Any],
        now: datetime,
    ) -> Completion:
        result_hash = payload_fingerprint(result)
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            job = session.get(AsyncJobRow, claim.job.job_id, with_for_update=True)
            attempt = session.get(AsyncJobAttemptRow, claim.attempt_id, with_for_update=True)
            if (
                job is not None
                and attempt is not None
                and job.status == JobState.SUCCEEDED.value
                and attempt.status == AttemptState.SUCCEEDED.value
            ):
                if payload_fingerprint(job.result) != result_hash:
                    raise IdempotencyConflict("completed job has a different result")
                return Completion(JobState.SUCCEEDED, duplicate=True)
            job, attempt = self._owned_attempt_rows(
                job=job, attempt=attempt, claim=claim, now=event_time
            )
            attempt.status = AttemptState.SUCCEEDED.value
            attempt.heartbeat_at = event_time
            attempt.lease_expires_at = None
            attempt.completed_at = event_time
            job.status = JobState.SUCCEEDED.value
            job.result = result
            job.last_error = None
            job.updated_at = event_time
            job.completed_at = event_time
            self._add_state_outbox(session, job=job, state=JobState.SUCCEEDED, now=event_time)
            return Completion(JobState.SUCCEEDED, duplicate=False)

    def fail(
        self,
        claim: DurableClaim,
        *,
        error: BaseException,
        retryable: bool,
        retry_delay_seconds: int,
        now: datetime,
    ) -> Completion:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be nonnegative")
        event_time = require_aware(now, field="now")
        message = safe_error(error)
        with self.session_factory.begin() as session:
            job = session.get(AsyncJobRow, claim.job.job_id, with_for_update=True)
            attempt = session.get(AsyncJobAttemptRow, claim.attempt_id, with_for_update=True)
            if attempt is not None and attempt.status == AttemptState.FAILED.value:
                if attempt.error != message:
                    raise IdempotencyConflict("failed attempt has a different error")
                if job is None:
                    raise LeaseLost("job no longer exists")
                return Completion(JobState(job.status), duplicate=True)
            job, attempt = self._owned_attempt_rows(
                job=job, attempt=attempt, claim=claim, now=event_time
            )
            attempt.status = AttemptState.FAILED.value
            attempt.error = message
            attempt.heartbeat_at = event_time
            attempt.lease_expires_at = None
            attempt.completed_at = event_time
            if retryable and claim.attempt < job.max_attempts:
                job.status = JobState.QUEUED.value
                job.due_at = event_time + timedelta(seconds=retry_delay_seconds)
                job.completed_at = None
                state = JobState.QUEUED
            else:
                job.status = JobState.FAILED.value
                job.completed_at = event_time
                state = JobState.FAILED
            job.last_error = message
            job.updated_at = event_time
            self._add_state_outbox(session, job=job, state=state, now=event_time)
            return Completion(state, duplicate=False)

    def cancel(self, job_id: uuid.UUID, *, now: datetime) -> Completion:
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            job = session.get(AsyncJobRow, job_id, with_for_update=True)
            if job is None:
                raise LookupError("job does not exist")
            state = JobState(job.status)
            if state == JobState.CANCELLED:
                return Completion(state, duplicate=True)
            if state in {JobState.SUCCEEDED, JobState.FAILED}:
                raise IdempotencyConflict(f"cannot cancel terminal {state.value} job")
            running_attempt = session.scalar(
                select(AsyncJobAttemptRow)
                .where(
                    AsyncJobAttemptRow.job_id == job_id,
                    AsyncJobAttemptRow.status == AttemptState.RUNNING.value,
                )
                .with_for_update()
            )
            if running_attempt is not None:
                running_attempt.status = AttemptState.CANCELLED.value
                running_attempt.error = "cancelled"
                running_attempt.heartbeat_at = event_time
                running_attempt.lease_expires_at = None
                running_attempt.completed_at = event_time
            job.status = JobState.CANCELLED.value
            job.last_error = "cancelled"
            job.updated_at = event_time
            job.completed_at = event_time
            self._add_state_outbox(session, job=job, state=JobState.CANCELLED, now=event_time)
            return Completion(JobState.CANCELLED, duplicate=False)

    def retry(self, job_id: uuid.UUID, *, due_at: datetime, now: datetime) -> DurableJob:
        event_time = require_aware(now, field="now")
        retry_at = require_aware(due_at, field="due_at")
        with self.session_factory.begin() as session:
            job = session.get(AsyncJobRow, job_id, with_for_update=True)
            if job is None:
                raise LookupError("job does not exist")
            if job.status != JobState.FAILED.value:
                raise IdempotencyConflict("only failed jobs can be retried")
            if job.attempt_count >= job.max_attempts:
                raise IdempotencyConflict("job exhausted its configured attempts")
            job.status = JobState.QUEUED.value
            job.due_at = retry_at
            job.completed_at = None
            job.updated_at = event_time
            self._add_state_outbox(session, job=job, state=JobState.QUEUED, now=event_time)
            return self._job_view(job)

    def claim_outbox(
        self, *, now: datetime, lease_seconds: int, limit: int = 100
    ) -> list[OutboxClaim]:
        if lease_seconds <= 0 or limit <= 0 or limit > 1000:
            raise ValueError("invalid outbox lease or limit")
        event_time = require_aware(now, field="now")
        lease_expires_at = event_time + timedelta(seconds=lease_seconds)
        claims: list[OutboxClaim] = []
        with self.session_factory.begin() as session:
            expired = list(
                session.scalars(
                    select(AsyncOutboxRow)
                    .where(
                        AsyncOutboxRow.status == OutboxState.DELIVERING.value,
                        AsyncOutboxRow.lease_expires_at <= event_time,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for row in expired:
                row.status = OutboxState.PENDING.value
                row.delivery_token = None
                row.lease_expires_at = None
            session.flush()
            rows = list(
                session.scalars(
                    select(AsyncOutboxRow)
                    .where(
                        AsyncOutboxRow.status == OutboxState.PENDING.value,
                        AsyncOutboxRow.available_at <= event_time,
                    )
                    .order_by(AsyncOutboxRow.outbox_id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for row in rows:
                token = uuid.uuid4()
                row.status = OutboxState.DELIVERING.value
                row.delivery_attempts += 1
                row.delivery_token = token
                row.lease_expires_at = lease_expires_at
                claims.append(
                    OutboxClaim(
                        outbox_id=row.outbox_id,
                        topic=row.topic,
                        payload=row.payload,
                        delivery_token=token,
                        delivery_attempt=row.delivery_attempts,
                        lease_expires_at=lease_expires_at,
                    )
                )
        return claims

    def ack_outbox(self, claim: OutboxClaim, *, now: datetime) -> bool:
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            row = session.get(AsyncOutboxRow, claim.outbox_id, with_for_update=True)
            if row is None:
                raise LookupError("outbox message does not exist")
            if row.status == OutboxState.PUBLISHED.value:
                return False
            if (
                row.status != OutboxState.DELIVERING.value
                or row.delivery_token != claim.delivery_token
            ):
                raise LeaseLost("outbox delivery token is no longer current")
            row.status = OutboxState.PUBLISHED.value
            row.published_at = event_time
            row.delivery_token = None
            row.lease_expires_at = None
            row.last_error = None
            return True

    def nack_outbox(
        self,
        claim: OutboxClaim,
        *,
        error: BaseException,
        now: datetime,
        retry_delay_seconds: int,
    ) -> None:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be nonnegative")
        event_time = require_aware(now, field="now")
        with self.session_factory.begin() as session:
            row = session.get(AsyncOutboxRow, claim.outbox_id, with_for_update=True)
            if row is None:
                raise LookupError("outbox message does not exist")
            if (
                row.status != OutboxState.DELIVERING.value
                or row.delivery_token != claim.delivery_token
            ):
                raise LeaseLost("outbox delivery token is no longer current")
            row.status = OutboxState.PENDING.value
            row.available_at = event_time + timedelta(seconds=retry_delay_seconds)
            row.delivery_token = None
            row.lease_expires_at = None
            row.last_error = safe_error(error)

    def _recover_expired(self, session: Session, *, now: datetime) -> None:
        attempts = list(
            session.scalars(
                select(AsyncJobAttemptRow)
                .where(
                    AsyncJobAttemptRow.status == AttemptState.RUNNING.value,
                    AsyncJobAttemptRow.lease_expires_at.is_not(None),
                    AsyncJobAttemptRow.lease_expires_at <= now,
                )
                .order_by(AsyncJobAttemptRow.lease_expires_at, AsyncJobAttemptRow.attempt_id)
                .with_for_update(skip_locked=True)
            )
        )
        for attempt in attempts:
            job = session.get(AsyncJobRow, attempt.job_id, with_for_update=True)
            if job is None or job.status != JobState.RUNNING.value:
                continue
            attempt.status = AttemptState.LEASE_EXPIRED.value
            attempt.error = "worker lease expired; recovered by database poll"
            attempt.heartbeat_at = now
            attempt.lease_expires_at = None
            attempt.completed_at = now
            job.last_error = attempt.error
            job.updated_at = now
            if attempt.attempt >= job.max_attempts:
                self._mark_exhausted(job, now=now)
                self._add_state_outbox(session, job=job, state=JobState.FAILED, now=now)
            else:
                job.status = JobState.QUEUED.value
                job.due_at = now

    @staticmethod
    def queued_claim_query(
        *, now: datetime, task_names: set[str] | None = None
    ) -> Select[tuple[AsyncJobRow]]:
        """Build the PostgreSQL claim statement for migration/integration tests."""

        event_time = require_aware(now, field="now")
        query: Select[tuple[AsyncJobRow]] = select(AsyncJobRow).where(
            AsyncJobRow.status == JobState.QUEUED.value,
            AsyncJobRow.due_at <= event_time,
        )
        if task_names is not None:
            query = query.where(AsyncJobRow.task_name.in_(sorted(task_names)))
        return (
            query.order_by(AsyncJobRow.due_at, AsyncJobRow.created_at, AsyncJobRow.job_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )

    @staticmethod
    def _mark_exhausted(job: AsyncJobRow, *, now: datetime) -> None:
        job.status = JobState.FAILED.value
        job.last_error = "max_attempts_exhausted"
        job.updated_at = now
        job.completed_at = now

    @staticmethod
    def _same_or_conflict(existing: AsyncJobRow, spec: JobSpec) -> DurableJob:
        if existing.payload_fingerprint != spec.fingerprint:
            raise IdempotencyConflict("idempotency_key has different immutable job input")
        if spec.job_id is not None and existing.job_id != spec.job_id:
            raise IdempotencyConflict("idempotency_key is already bound to another job_id")
        return SqlAlchemyAsyncRepository._job_view(existing)

    def _owned_attempt(
        self, session: Session, *, claim: DurableClaim, now: datetime
    ) -> tuple[AsyncJobRow, AsyncJobAttemptRow]:
        job = session.get(AsyncJobRow, claim.job.job_id, with_for_update=True)
        attempt = session.get(AsyncJobAttemptRow, claim.attempt_id, with_for_update=True)
        return self._owned_attempt_rows(job=job, attempt=attempt, claim=claim, now=now)

    @staticmethod
    def _owned_attempt_rows(
        *,
        job: AsyncJobRow | None,
        attempt: AsyncJobAttemptRow | None,
        claim: DurableClaim,
        now: datetime,
    ) -> tuple[AsyncJobRow, AsyncJobAttemptRow]:
        if job is None or attempt is None or attempt.job_id != claim.job.job_id:
            raise LeaseLost("job attempt does not exist")
        if (
            job.status != JobState.RUNNING.value
            or attempt.status != AttemptState.RUNNING.value
            or attempt.worker_id != claim.worker_id
            or attempt.fence_token != claim.fence_token
        ):
            raise LeaseLost("worker no longer owns this attempt")
        if attempt.lease_expires_at is None or ensure_utc(attempt.lease_expires_at) <= now:
            raise LeaseLost("worker lease expired")
        return job, attempt

    @staticmethod
    def _add_state_outbox(
        session: Session, *, job: AsyncJobRow, state: JobState, now: datetime
    ) -> None:
        session.add(
            AsyncOutboxRow(
                idempotency_key=f"job:{job.job_id}:{state.value}:{job.attempt_count}",
                topic=f"async.job.{state.value}",
                payload={
                    "job_id": str(job.job_id),
                    "task_name": job.task_name,
                    "state": state.value,
                    "attempt": job.attempt_count,
                },
                status=OutboxState.PENDING.value,
                available_at=now,
                created_at=now,
            )
        )

    @staticmethod
    def _job_view(row: AsyncJobRow) -> DurableJob:
        return DurableJob(
            job_id=row.job_id,
            idempotency_key=row.idempotency_key,
            task_name=row.task_name,
            payload=row.payload,
            payload_fingerprint=row.payload_fingerprint,
            state=JobState(row.status),
            due_at=ensure_utc(row.due_at),
            max_attempts=row.max_attempts,
            attempt_count=row.attempt_count,
            created_at=ensure_utc(row.created_at),
            updated_at=ensure_utc(row.updated_at),
            result=row.result,
            last_error=row.last_error,
            completed_at=ensure_utc(row.completed_at) if row.completed_at else None,
        )
