from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.auth.errors import ApiError
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    ModelStatus,
    ModelVersion,
    TrainingJob,
    TrainingJobStatus,
)

MODEL_TRANSITIONS: dict[ModelStatus, set[ModelStatus]] = {
    ModelStatus.TRAINING: {ModelStatus.EVALUATED, ModelStatus.FAILED},
    ModelStatus.EVALUATED: {ModelStatus.READY, ModelStatus.FAILED},
    ModelStatus.READY: {ModelStatus.ACTIVE, ModelStatus.FAILED},
    ModelStatus.ACTIVE: {ModelStatus.ARCHIVED, ModelStatus.FAILED},
    ModelStatus.ARCHIVED: {ModelStatus.READY},
    ModelStatus.FAILED: set(),
}

JOB_TRANSITIONS: dict[TrainingJobStatus, set[TrainingJobStatus]] = {
    TrainingJobStatus.QUEUED: {
        TrainingJobStatus.RUNNING,
        TrainingJobStatus.CANCELLED,
        TrainingJobStatus.FAILED,
    },
    TrainingJobStatus.RUNNING: {
        TrainingJobStatus.SUCCEEDED,
        TrainingJobStatus.FAILED,
        TrainingJobStatus.CANCELLED,
    },
    TrainingJobStatus.SUCCEEDED: set(),
    TrainingJobStatus.FAILED: set(),
    TrainingJobStatus.CANCELLED: set(),
}


class ModelRegistryRepository:
    def add_model(self, session: Session, model: ModelVersion) -> ModelVersion:
        self._validate_eligibility(model)
        session.add(model)
        session.flush()
        return model

    def transition_model(
        self,
        session: Session,
        *,
        version: str,
        target: ModelStatus,
        failure_reason: str | None = None,
    ) -> ModelVersion:
        model = session.get(ModelVersion, version, with_for_update=True)
        if model is None:
            raise LookupError("model version does not exist")
        if target not in MODEL_TRANSITIONS[model.status]:
            raise ValueError(f"invalid model transition {model.status.value}->{target.value}")
        if target in {ModelStatus.READY, ModelStatus.ACTIVE}:
            self._validate_eligibility(model)
            if not model.activation_eligible:
                raise ValueError("model is not activation eligible")
        model.status = target
        model.failure_reason = failure_reason
        session.flush()
        return model

    def list_models(self, session: Session) -> list[ModelVersion]:
        return list(
            session.scalars(
                select(ModelVersion).order_by(ModelVersion.trained_at.desc().nulls_last())
            )
        )

    def enqueue_job(self, session: Session, job: TrainingJob) -> TrainingJob:
        self._validate_job_eligibility(job)
        existing = session.scalar(
            select(TrainingJob).where(TrainingJob.idempotency_key == job.idempotency_key)
        )
        if existing is not None:
            return self._existing_job(existing, job)
        try:
            with session.begin_nested():
                session.add(job)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(TrainingJob).where(TrainingJob.idempotency_key == job.idempotency_key)
            )
            if existing is None:
                raise
            return self._existing_job(existing, job)
        return job

    def transition_job(
        self,
        session: Session,
        *,
        job_id: uuid.UUID,
        target: TrainingJobStatus,
        failure_reason: str | None = None,
    ) -> TrainingJob:
        job = session.get(TrainingJob, job_id, with_for_update=True)
        if job is None:
            raise LookupError("training job does not exist")
        if target not in JOB_TRANSITIONS[job.status]:
            raise ValueError(f"invalid job transition {job.status.value}->{target.value}")
        job.status = target
        job.failure_reason = failure_reason
        session.flush()
        return job

    def list_jobs(self, session: Session) -> list[TrainingJob]:
        return list(session.scalars(select(TrainingJob).order_by(TrainingJob.created_at.desc())))

    @staticmethod
    def _validate_eligibility(model: ModelVersion) -> None:
        if model.purpose == EvaluationPurpose.SYSTEMS_ONLY:
            if (
                model.evaluation_comparability != Comparability.NON_COMPARABLE
                or model.activation_eligible
                or model.status in {ModelStatus.READY, ModelStatus.ACTIVE}
            ):
                raise ValueError("systems_only model cannot be comparable, ready, or active")
        if model.activation_eligible and model.evaluation_comparability != Comparability.COMPARABLE:
            raise ValueError("activation eligibility requires comparable evaluation")

    @staticmethod
    def _validate_job_eligibility(job: TrainingJob) -> None:
        if job.purpose == EvaluationPurpose.SYSTEMS_ONLY and (
            job.evaluation_comparability != Comparability.NON_COMPARABLE or job.activation_eligible
        ):
            raise ValueError("systems_only job must be non-comparable and ineligible")
        if job.activation_eligible and job.evaluation_comparability != Comparability.COMPARABLE:
            raise ValueError("activation eligibility requires comparable evaluation")

    @staticmethod
    def _existing_job(existing: TrainingJob, requested: TrainingJob) -> TrainingJob:
        identity = (
            "data_version",
            "data_manifest_checksum",
            "config_checksum",
            "purpose",
            "evaluation_comparability",
            "activation_eligible",
        )
        if any(getattr(existing, field) != getattr(requested, field) for field in identity):
            raise ApiError(
                409,
                "training_job_idempotency_conflict",
                "idempotency_key has different training job content",
            )
        return existing
