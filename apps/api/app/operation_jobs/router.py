# ruff: noqa: B008
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status

from apps.api.app.async_runtime.domain import DurableJob, IdempotencyConflict
from apps.api.app.async_runtime.repository import SqlAlchemyAsyncRepository
from apps.api.app.async_runtime.schemas import DurableJobResponse
from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import Role

from .domain import OperationJobSpec, OperationKind, TargetExpectation
from .schemas import OperationJobCreateRequest, OperationJobResponse
from .service import OPERATION_TASK_NAME, OperationJobService

READ_ROLES = (Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)
WRITE_ROLES = (Role.OPERATOR, Role.ADMIN)


def build_operation_jobs_router(
    *,
    dependencies: AuthDependencies,
    service: OperationJobService,
    repository: SqlAlchemyAsyncRepository,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/admin/operation-jobs", tags=["admin", "operations"])

    @router.post(
        "",
        response_model=OperationJobResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="createOperationJob",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def create_operation_job(
        payload: OperationJobCreateRequest,
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> OperationJobResponse:
        now = _clock(clock)
        operation_payload = {
            "authorized_submission": {
                "actor_id": str(authenticated.user.id),
                "actor_role": authenticated.user.role.value,
                "authorized_at": now.isoformat(),
            },
            "scope_type": payload.scope_type,
            "scope_value": payload.scope_value,
            "starts_at_utc": payload.due_at.astimezone(UTC).isoformat(),
            "ends_at_utc": (
                payload.ends_at_utc.astimezone(UTC).isoformat()
                if payload.ends_at_utc is not None
                else None
            ),
            "priority": payload.priority,
            "target_position": payload.target_position,
            "reason": payload.reason,
        }
        try:
            job, created = service.submit(
                OperationJobSpec(
                    operation_id=payload.operation_id,
                    idempotency_key=payload.idempotency_key,
                    kind=OperationKind(payload.kind),
                    targets=tuple(
                        TargetExpectation(target.target_id, target.state_version)
                        for target in payload.targets
                    ),
                    due_at=payload.due_at,
                    payload=operation_payload,
                    max_attempts=payload.max_attempts,
                ),
                now=now,
            )
        except IdempotencyConflict as exc:
            raise ApiError(409, "operation_job_conflict", str(exc)) from exc
        except ValueError as exc:
            raise ApiError(422, "invalid_operation_job", str(exc)) from exc
        return OperationJobResponse(created=created, job=_response(job))

    @router.get(
        "/{operation_id}",
        response_model=DurableJobResponse,
        operation_id="getOperationJob",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def get_operation_job(
        operation_id: uuid.UUID,
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> DurableJobResponse:
        return _response(_get_operation(repository, operation_id))

    @router.post(
        "/{operation_id}/cancel",
        response_model=OperationJobResponse,
        operation_id="cancelOperationJob",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def cancel_operation_job(
        operation_id: uuid.UUID,
        _authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> OperationJobResponse:
        try:
            completion = service.cancel(operation_id, now=_clock(clock))
        except LookupError as exc:
            raise ApiError(404, "operation_job_not_found", "Operation job does not exist") from exc
        except IdempotencyConflict as exc:
            raise ApiError(409, "operation_job_conflict", str(exc)) from exc
        return OperationJobResponse(
            duplicate=completion.duplicate,
            job=_response(_get_operation(repository, operation_id)),
        )

    return router


def _get_operation(repository: SqlAlchemyAsyncRepository, operation_id: uuid.UUID) -> DurableJob:
    job = repository.get(operation_id)
    if job is None or job.task_name != OPERATION_TASK_NAME:
        raise ApiError(404, "operation_job_not_found", "Operation job does not exist")
    return job


def _response(job: DurableJob) -> DurableJobResponse:
    return DurableJobResponse(
        job_id=job.job_id,
        idempotency_key=job.idempotency_key,
        task_name=job.task_name,
        state=job.state.value,
        due_at=job.due_at,
        max_attempts=job.max_attempts,
        attempt_count=job.attempt_count,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
        result=job.result,
        last_error=job.last_error,
    )


def _clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("operation API clock must be timezone-aware")
    return value.astimezone(UTC)
