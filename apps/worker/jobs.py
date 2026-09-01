from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.db.base import ensure_utc
from apps.api.app.db.models import (
    EvaluationPurpose,
    JobAttempt,
    TrainingJob,
    TrainingJobStatus,
)
from apps.api.app.models_registry.repository import ModelRegistryRepository

from .contracts import Broker, LeaseLost, TrainingRequest

DATA_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


@dataclass(frozen=True)
class Claim:
    request: TrainingRequest
    lease_expires_at: datetime


class JobCoordinator:
    """PostgreSQL-authoritative training job state machine.

    Redis only wakes workers. Claims use row locks, leases live in JobAttempt, and an
    expired RUNNING attempt is recovered before a new QUEUED job is claimed.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        broker: Broker | None = None,
        lease_seconds: int = 60,
        max_attempts: int = 3,
        repository: ModelRegistryRepository | None = None,
    ) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("lease_seconds and max_attempts must be positive")
        self.session_factory = session_factory
        self.broker = broker
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.repository = repository or ModelRegistryRepository()

    def enqueue(self, job: TrainingJob) -> TrainingJob:
        self._validate_job_identity(job)
        with self.session_factory.begin() as session:
            queued = self.repository.enqueue_job(session, job)
            job_id = queued.job_id
        self._notify(job_id)
        return queued

    def claim_next(self, *, worker_id: str, now: datetime) -> Claim | None:
        event_time = _clock_utc(now)
        with self.session_factory.begin() as session:
            self._recover_expired(session, now=event_time)
            # The project session intentionally disables autoflush. Persist recovered
            # RUNNING -> QUEUED rows before selecting the next claim candidate.
            session.flush()
            job = session.scalar(
                select(TrainingJob)
                .where(TrainingJob.status == TrainingJobStatus.QUEUED)
                .order_by(TrainingJob.created_at, TrainingJob.job_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            attempt_number = (
                int(
                    session.scalar(
                        select(func.max(JobAttempt.attempt)).where(JobAttempt.job_id == job.job_id)
                    )
                    or 0
                )
                + 1
            )
            if attempt_number > self.max_attempts:
                job.status = TrainingJobStatus.FAILED
                job.failure_reason = "max_attempts_exhausted"
                job.completed_at = event_time
                return None
            lease_expires_at = event_time + timedelta(seconds=self.lease_seconds)
            attempt = JobAttempt(
                job_id=job.job_id,
                worker_id=worker_id,
                attempt=attempt_number,
                lease_expires_at=lease_expires_at,
                heartbeat_at=event_time,
                status="running",
            )
            session.add(attempt)
            job.status = TrainingJobStatus.RUNNING
            job.failure_reason = None
            session.flush()
            return Claim(
                request=TrainingRequest(
                    job_id=job.job_id,
                    attempt_id=attempt.id,
                    attempt=attempt_number,
                    data_version=job.data_version,
                    data_manifest_checksum=job.data_manifest_checksum,
                    config_checksum=job.config_checksum,
                    purpose=job.purpose,
                    evaluation_comparability=job.evaluation_comparability,
                    activation_eligible=job.activation_eligible,
                ),
                lease_expires_at=lease_expires_at,
            )

    def heartbeat(
        self,
        *,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        worker_id: str,
        now: datetime,
    ) -> datetime:
        event_time = _clock_utc(now)
        with self.session_factory.begin() as session:
            job, attempt = self._owned_running_attempt(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=worker_id,
                now=event_time,
            )
            if job.status == TrainingJobStatus.CANCELLED:
                raise LeaseLost("job was cancelled")
            attempt.heartbeat_at = event_time
            attempt.lease_expires_at = event_time + timedelta(seconds=self.lease_seconds)
            return attempt.lease_expires_at

    def cancellation_requested(self, *, job_id: uuid.UUID) -> bool:
        with self.session_factory() as session:
            status = session.scalar(select(TrainingJob.status).where(TrainingJob.job_id == job_id))
            return status == TrainingJobStatus.CANCELLED

    def cancel(self, *, job_id: uuid.UUID, now: datetime) -> TrainingJobStatus:
        event_time = _clock_utc(now)
        with self.session_factory.begin() as session:
            job = session.get(TrainingJob, job_id, with_for_update=True)
            if job is None:
                raise LookupError("training job does not exist")
            if job.status in {
                TrainingJobStatus.SUCCEEDED,
                TrainingJobStatus.FAILED,
                TrainingJobStatus.CANCELLED,
            }:
                return job.status
            job.status = TrainingJobStatus.CANCELLED
            job.completed_at = event_time
            running_attempts = session.scalars(
                select(JobAttempt).where(
                    JobAttempt.job_id == job_id,
                    JobAttempt.status == "running",
                )
            )
            for attempt in running_attempts:
                attempt.status = "cancelled"
                attempt.error = "cancelled"
                attempt.heartbeat_at = event_time
                attempt.lease_expires_at = None
            return job.status

    def succeed(
        self,
        *,
        claim: Claim,
        worker_id: str,
        result: dict[str, Any],
        now: datetime,
    ) -> None:
        event_time = _clock_utc(now)
        self._validate_result(claim.request, result)
        with self.session_factory.begin() as session:
            job, attempt = self._owned_running_attempt(
                session,
                job_id=claim.request.job_id,
                attempt_id=claim.request.attempt_id,
                worker_id=worker_id,
                now=event_time,
            )
            attempt.status = "succeeded"
            attempt.result = result
            attempt.heartbeat_at = event_time
            attempt.lease_expires_at = None
            job.status = TrainingJobStatus.SUCCEEDED
            job.failure_reason = None
            job.completed_at = event_time

    def fail(
        self,
        *,
        claim: Claim,
        worker_id: str,
        error: BaseException,
        retryable: bool,
        now: datetime,
    ) -> TrainingJobStatus:
        event_time = _clock_utc(now)
        safe_error = f"{type(error).__name__}: {error}"[:2000]
        with self.session_factory.begin() as session:
            job, attempt = self._owned_running_attempt(
                session,
                job_id=claim.request.job_id,
                attempt_id=claim.request.attempt_id,
                worker_id=worker_id,
                now=event_time,
            )
            attempt.status = "failed"
            attempt.error = safe_error
            attempt.heartbeat_at = event_time
            attempt.lease_expires_at = None
            if retryable and claim.request.attempt < self.max_attempts:
                job.status = TrainingJobStatus.QUEUED
                job.failure_reason = safe_error
                job.completed_at = None
            else:
                job.status = TrainingJobStatus.FAILED
                job.failure_reason = safe_error
                job.completed_at = event_time
            final_status = job.status
        if final_status == TrainingJobStatus.QUEUED:
            self._notify(claim.request.job_id)
        return final_status

    def _recover_expired(self, session: Session, *, now: datetime) -> None:
        expired_attempts = list(
            session.scalars(
                select(JobAttempt)
                .join(TrainingJob, TrainingJob.job_id == JobAttempt.job_id)
                .where(
                    TrainingJob.status == TrainingJobStatus.RUNNING,
                    JobAttempt.status == "running",
                    JobAttempt.lease_expires_at.is_not(None),
                    JobAttempt.lease_expires_at <= now,
                )
                .order_by(JobAttempt.lease_expires_at, JobAttempt.id)
                .with_for_update(skip_locked=True)
            )
        )
        for attempt in expired_attempts:
            job = session.get(TrainingJob, attempt.job_id, with_for_update=True)
            if job is None or job.status != TrainingJobStatus.RUNNING:
                continue
            attempt.status = "lease_expired"
            attempt.error = "worker lease expired; recovered by another run"
            attempt.heartbeat_at = now
            attempt.lease_expires_at = None
            if attempt.attempt >= self.max_attempts:
                job.status = TrainingJobStatus.FAILED
                job.failure_reason = "max_attempts_exhausted_after_lease_expiry"
                job.completed_at = now
            else:
                job.status = TrainingJobStatus.QUEUED
                job.failure_reason = "previous_attempt_lease_expired"

    @staticmethod
    def _owned_running_attempt(
        session: Session,
        *,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        worker_id: str,
        now: datetime,
    ) -> tuple[TrainingJob, JobAttempt]:
        job = session.get(TrainingJob, job_id, with_for_update=True)
        attempt = session.get(JobAttempt, attempt_id, with_for_update=True)
        if job is None or attempt is None or attempt.job_id != job_id:
            raise LeaseLost("job attempt does not exist")
        if (
            job.status != TrainingJobStatus.RUNNING
            or attempt.status != "running"
            or attempt.worker_id != worker_id
        ):
            raise LeaseLost("worker no longer owns this running attempt")
        if attempt.lease_expires_at is None or ensure_utc(attempt.lease_expires_at) <= now:
            raise LeaseLost("worker lease expired")
        return job, attempt

    @staticmethod
    def _validate_job_identity(job: TrainingJob) -> None:
        if not DATA_VERSION_PATTERN.fullmatch(job.data_version) or job.data_version.lower() in {
            "latest",
            ".",
            "..",
        }:
            raise ValueError("training requires an explicit immutable data_version, never latest")
        for field in ("data_manifest_checksum", "config_checksum"):
            value = getattr(job, field)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{field} must be a lowercase SHA-256")

    @staticmethod
    def _validate_result(request: TrainingRequest, result: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            raise ValueError("training handler result must be an object")
        if result.get("published") or result.get("activated"):
            raise ValueError("worker handlers may not publish or activate models")
        if request.purpose == EvaluationPurpose.SYSTEMS_ONLY:
            status = str(result.get("model_status", "EVALUATED")).upper()
            if status in {"READY", "ACTIVE"} or result.get("activation_eligible"):
                raise ValueError("systems_only results cannot be ready or active")

    def _notify(self, job_id: uuid.UUID) -> None:
        if self.broker is None:
            return
        try:
            self.broker.notify(job_id)
        except Exception:
            # PostgreSQL is authoritative. A DB polling pass will still find the job.
            return


def _clock_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("worker clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
