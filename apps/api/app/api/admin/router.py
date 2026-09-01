# ruff: noqa: B008
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import FeedType, Role

from .csv_export import dashboard_csv
from .queries import DashboardQueryService
from .schemas import (
    DashboardBucket,
    DashboardFeedDiagnostics,
    DashboardOverview,
    HotItem,
    RecommendationRequestDebugResponse,
    UserDebugResponse,
)

# FastAPI dependency markers are intentionally evaluated in route signatures.

READ_ROLES = (Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)


def build_dashboard_router(
    *,
    get_session: Callable[..., Session],
    dependencies: AuthDependencies,
    queries: DashboardQueryService,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get(
        "/dashboard/overview",
        response_model=DashboardOverview,
        operation_id="getDashboardOverview",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def overview(
        from_utc: datetime,
        to_utc: datetime,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> DashboardOverview:
        return queries.overview(session, from_utc=from_utc, to_utc=to_utc)

    @router.get(
        "/dashboard/timeseries",
        response_model=list[DashboardBucket],
        operation_id="getDashboardTimeseries",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def timeseries(
        from_utc: datetime,
        to_utc: datetime,
        feed_type: FeedType | None = None,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> list[DashboardBucket]:
        return queries.timeseries(session, from_utc=from_utc, to_utc=to_utc, feed_type=feed_type)

    @router.get(
        "/dashboard/feeds",
        response_model=DashboardFeedDiagnostics,
        operation_id="getDashboardFeeds",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def feeds(
        from_utc: datetime,
        to_utc: datetime,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> DashboardFeedDiagnostics:
        return queries.feeds(session, from_utc=from_utc, to_utc=to_utc)

    @router.get(
        "/dashboard/export.csv",
        response_class=Response,
        operation_id="exportDashboardCsv",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def export_csv(
        from_utc: datetime,
        to_utc: datetime,
        feed_type: FeedType | None = None,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> Response:
        buckets = queries.timeseries(session, from_utc=from_utc, to_utc=to_utc, feed_type=feed_type)
        return Response(
            content=dashboard_csv(buckets),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="dashboard.csv"'},
        )

    @router.get(
        "/dashboard/hot-items",
        response_model=list[HotItem],
        operation_id="getDashboardHotItems",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def hot_items(
        from_utc: datetime,
        to_utc: datetime,
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> list[HotItem]:
        return queries.hot_items(session, from_utc=from_utc, to_utc=to_utc, limit=limit)

    @router.get(
        "/users/{user_id}/debug",
        response_model=UserDebugResponse,
        operation_id="debugUser",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def debug_user(
        user_id: uuid.UUID,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> UserDebugResponse:
        return queries.user_debug(session, user_id)

    @router.get(
        "/requests/{request_id}",
        response_model=RecommendationRequestDebugResponse,
        operation_id="debugRecommendationRequest",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def debug_request(
        request_id: uuid.UUID,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> RecommendationRequestDebugResponse:
        return queries.request_debug(session, request_id)

    return router
