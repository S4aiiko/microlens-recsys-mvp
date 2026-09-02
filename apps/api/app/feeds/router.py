# ruff: noqa: B008
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError, ErrorEnvelope
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import FeedType, Role

from .cursor import CursorError
from .schemas import FeedPage
from .service import RecommendationService

ALL_ROLES = (Role.USER, Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)


def build_feeds_router(
    *,
    get_session: Callable[..., Session],
    dependencies: AuthDependencies,
    service: RecommendationService,
) -> APIRouter:
    """Return the Phase 4 router for the Integration owner to mount in main.py."""

    router = APIRouter(tags=["feeds"])

    @router.get(
        "/api/feeds/{feed_type}",
        response_model=FeedPage,
        operation_id="getFeedPage",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in ALL_ROLES],
            "x-implementation-phase": "phase_4",
        },
        responses={
            401: {"model": ErrorEnvelope, "description": "Authentication required"},
            403: {"model": ErrorEnvelope, "description": "Feed authority denied"},
            409: {"model": ErrorEnvelope, "description": "Feed generation conflict"},
            422: {"model": ErrorEnvelope, "description": "Invalid feed request or cursor"},
        },
    )
    def get_feed_page(
        feed_type: FeedType,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=4096),
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.roles(*ALL_ROLES)),
    ) -> FeedPage:
        try:
            result = service.get_page(
                session,
                user_id=authenticated.user.id,
                feed_type=feed_type,
                limit=limit,
                cursor=cursor,
            )
        except CursorError as exc:
            session.rollback()
            raise ApiError(422, "invalid_cursor", str(exc)) from exc
        except PermissionError as exc:
            session.rollback()
            raise ApiError(403, "feed_authority_denied", str(exc)) from exc
        except ValueError as exc:
            session.rollback()
            raise ApiError(409, "feed_snapshot_conflict", str(exc)) from exc
        session.commit()
        return result.page

    return router
