# ruff: noqa: B008
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import Comparability, ModelVersion, Role, TrainingJob

from .repository import ModelRegistryRepository
from .schemas import (
    ActivationRequest,
    ModelComparisonResponse,
    ModelVersionResponse,
    TrainingJobResponse,
)
from .service import ActivationService, RuntimeModelSlot

# FastAPI dependency markers are intentionally evaluated in route signatures.

READ_ROLES = (Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)


def model_response(model: ModelVersion) -> ModelVersionResponse:
    return ModelVersionResponse(
        model_version=model.model_version,
        data_version=model.data_version,
        status=model.status.value,
        purpose=model.purpose.value,
        evaluation_comparability=model.evaluation_comparability.value,
        activation_eligible=model.activation_eligible,
        metrics=model.metrics,
        trained_at=model.trained_at,
        published_at=model.published_at,
    )


def job_response(job: TrainingJob) -> TrainingJobResponse:
    return TrainingJobResponse(
        job_id=job.job_id,
        idempotency_key=job.idempotency_key,
        status=job.status.value,
        data_version=job.data_version,
        data_manifest_checksum=job.data_manifest_checksum,
        purpose=job.purpose.value,
        evaluation_comparability=job.evaluation_comparability.value,
        activation_eligible=job.activation_eligible,
        failure_reason=job.failure_reason,
    )


def build_model_admin_router(
    *,
    get_session: Callable[..., Session],
    dependencies: AuthDependencies,
    repository: ModelRegistryRepository,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get(
        "/models",
        response_model=list[ModelVersionResponse],
        operation_id="listModelVersions",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def list_models(
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> list[ModelVersionResponse]:
        return [model_response(model) for model in repository.list_models(session)]

    @router.get(
        "/models/compare",
        response_model=ModelComparisonResponse,
        operation_id="compareModelVersions",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def compare_models(
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> ModelComparisonResponse:
        models = repository.list_models(session)[:2]
        if len(models) < 2:
            raise ApiError(422, "not_enough_model_versions", "At least two versions are required")
        comparable = (
            all(model.evaluation_comparability == Comparability.COMPARABLE for model in models)
            and len({model.data_version for model in models}) == 1
        )
        return ModelComparisonResponse(
            versions=[model_response(model) for model in models],
            comparable=comparable,
            reason=None if comparable else "versions are not comparable on the same data version",
        )

    @router.get(
        "/training-jobs",
        response_model=list[TrainingJobResponse],
        operation_id="listTrainingJobs",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def list_training_jobs(
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> list[TrainingJobResponse]:
        return [job_response(job) for job in repository.list_jobs(session)]

    return router


def build_internal_activation_router(
    *,
    get_session: Callable[..., Session],
    service: ActivationService,
    runtime: RuntimeModelSlot,
) -> APIRouter:
    """Return an internal-only router; the public app must never include it."""

    router = APIRouter(prefix="/internal", tags=["internal"])

    @router.post(
        "/model-versions/{version}/activate",
        response_model=ModelVersionResponse,
        operation_id="activateModelVersion",
        openapi_extra={
            "security": [{"publishToken": []}],
            "x-internal-only": True,
        },
    )
    def activate_model(
        version: str,
        payload: ActivationRequest,
        x_publish_token: str | None = Header(default=None, alias="X-Publish-Token"),
        session: Session = Depends(get_session),
    ) -> ModelVersionResponse:
        # Authentication must precede every version-dependent query or audit mutation.
        service.authenticate_publish_token(x_publish_token)
        attempt = service.begin_attempt(
            session,
            version=version,
            expected_current_version=payload.expected_current_version,
        )
        try:
            prepared = service.prepare(
                session, version=version, manifest_checksum=payload.manifest_checksum
            )
        except ApiError as exc:
            service.record_failure(
                session,
                attempt_id=attempt.id,
                code=exc.code,
                reason=exc.message,
            )
            session.commit()
            raise
        session.commit()  # staging is complete; retain STARTED audit before the lock/CAS.
        try:
            with session.begin():
                plan = service.activate_prepared(
                    session,
                    prepared=prepared,
                    expected_current_version=payload.expected_current_version,
                    attempt_id=attempt.id,
                )
        except ApiError as exc:
            session.rollback()
            with session.begin():
                service.record_failure(
                    session,
                    attempt_id=attempt.id,
                    code=exc.code,
                    reason=exc.message,
                )
            raise
        # Staging must make this swap an in-memory atomic assignment with no I/O/failure path.
        runtime.swap(model_version=plan.model.model_version, staged_bundle=plan.staged_bundle)
        return model_response(plan.model)

    return router
