# ruff: noqa: B008
from __future__ import annotations

import uuid
from collections.abc import Callable, Collection
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import Role

from .domain import DurableJob, IdempotencyConflict, JobSpec
from .repository import SqlAlchemyAsyncRepository
from .schemas import (
    DurableJobResponse,
    JobCreateRequest,
    JobMutationResponse,
    JobRetryRequest,
)
from .service import DurableJobService

READ_ROLES = (Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)
WRITE_ROLES = (Role.OPERATOR, Role.ADMIN)


def build_async_jobs_router(
    *,
    dependencies: AuthDependencies,
    jobs: DurableJobService,
    repository: SqlAlchemyAsyncRepository,
    allowed_task_names: Collection[str],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    allowed = frozenset(allowed_task_names)
    if not allowed or any(not value or len(value) > 128 for value in allowed):
        raise ValueError("allowed_task_names must contain bounded task names")
    router = APIRouter(prefix="/api/admin/async-jobs", tags=["admin", "async-jobs"])

    @router.post(
        "",
        response_model=JobMutationResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="createAsyncJob",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def create_job(
        payload: JobCreateRequest,
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> JobMutationResponse:
        if payload.task_name not in allowed:
            raise ApiError(422, "unsupported_task", "Task type is not enabled by this worker")
        now = _clock(clock)
        authorized_payload = {
            "schema_version": 1,
            "authorized_submission": {
                "actor_id": str(authenticated.user.id),
                "actor_role": authenticated.user.role.value,
                "authorized_at": now.isoformat(),
            },
            "task_payload": payload.payload,
        }
        try:
            job, created = jobs.enqueue(
                JobSpec(
                    idempotency_key=payload.idempotency_key,
                    task_name=payload.task_name,
                    payload=authorized_payload,
                    due_at=payload.due_at,
                    max_attempts=payload.max_attempts,
                ),
                now=now,
            )
        except (IdempotencyConflict, ValueError) as exc:
            raise _write_error(exc) from exc
        return JobMutationResponse(created=created, job=_response(job))

    @router.get(
        "/{job_id}",
        response_model=DurableJobResponse,
        operation_id="getAsyncJob",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def get_job(
        job_id: uuid.UUID,
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> DurableJobResponse:
        return _response(_get(repository, job_id))

    @router.post(
        "/{job_id}/cancel",
        response_model=JobMutationResponse,
        operation_id="cancelAsyncJob",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def cancel_job(
        job_id: uuid.UUID,
        _authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> JobMutationResponse:
        try:
            completion = repository.cancel(job_id, now=_clock(clock))
        except LookupError as exc:
            raise ApiError(404, "job_not_found", "Async job does not exist") from exc
        except IdempotencyConflict as exc:
            raise ApiError(409, "job_state_conflict", str(exc)) from exc
        return JobMutationResponse(
            duplicate=completion.duplicate,
            job=_response(_get(repository, job_id)),
        )

    @router.post(
        "/{job_id}/retry",
        response_model=JobMutationResponse,
        operation_id="retryAsyncJob",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def retry_job(
        job_id: uuid.UUID,
        payload: JobRetryRequest,
        _authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> JobMutationResponse:
        try:
            job = repository.retry(job_id, due_at=payload.due_at, now=_clock(clock))
        except LookupError as exc:
            raise ApiError(404, "job_not_found", "Async job does not exist") from exc
        except (IdempotencyConflict, ValueError) as exc:
            raise _write_error(exc) from exc
        return JobMutationResponse(duplicate=False, job=_response(job))

    return router


def _get(repository: SqlAlchemyAsyncRepository, job_id: uuid.UUID) -> DurableJob:
    job = repository.get(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "Async job does not exist")
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
        raise RuntimeError("async API clock must be timezone-aware")
    return value.astimezone(UTC)


def _write_error(exc: BaseException) -> ApiError:
    if isinstance(exc, IdempotencyConflict):
        return ApiError(409, "job_state_conflict", str(exc))
    return ApiError(422, "invalid_job", str(exc))
