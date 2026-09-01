# ruff: noqa: B008
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import Role, UserProfile

from .schemas import (
    EventBatchRequest,
    EventBatchResponse,
    EventItemResult,
    EventRequest,
    UserProfileResponse,
)
from .service import EventService, profile_response

# FastAPI dependency markers are intentionally evaluated in route signatures.

ALL_ROLES = (Role.USER, Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)


def build_events_router(
    *,
    get_session: Callable[..., Session],
    dependencies: AuthDependencies,
    service: EventService,
) -> APIRouter:
    router = APIRouter(tags=["events"])

    @router.post(
        "/api/events",
        response_model=EventItemResult,
        operation_id="createEvent",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in ALL_ROLES],
        },
    )
    def create_event(
        payload: EventRequest,
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*ALL_ROLES)),
    ) -> EventItemResult:
        result = service.submit(session, user_id=authenticated.user.id, request=payload)
        if result.status == "rejected":
            session.rollback()
            raise ApiError(
                409 if result.error_code == "event_id_conflict" else 422,
                result.error_code or "event_rejected",
                result.message or "Event was rejected",
            )
        session.commit()
        return result

    @router.post(
        "/api/events/batch",
        response_model=EventBatchResponse,
        operation_id="createEventBatch",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in ALL_ROLES],
        },
    )
    def create_event_batch(
        payload: EventBatchRequest,
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*ALL_ROLES)),
    ) -> EventBatchResponse:
        response = service.submit_batch(session, user_id=authenticated.user.id, request=payload)
        session.commit()
        return response

    @router.get(
        "/api/profile/me",
        response_model=UserProfileResponse,
        operation_id="getMyProfile",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in ALL_ROLES],
        },
    )
    def my_profile(
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.roles(*ALL_ROLES)),
    ) -> UserProfileResponse:
        profile = session.get(UserProfile, authenticated.user.id)
        if profile is None:
            raise ApiError(404, "profile_not_found", "Profile does not exist")
        return profile_response(profile)

    return router
