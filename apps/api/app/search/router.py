# ruff: noqa: B008
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import Role

from .domain import AuthorityUnavailable, SearchPermissionDenied, SearchPrincipal, SearchQuery
from .health import SearchHealthService
from .schemas import ItemSearchResponse, SearchHealthResponse, SearchItemResponse
from .service import AuthoritativeSearchService

ALL_ROLES = (Role.USER, Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)
READ_ROLES = (Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)


def build_search_router(
    *,
    dependencies: AuthDependencies,
    service: AuthoritativeSearchService,
    health_service: SearchHealthService,
) -> APIRouter:
    """Build static item search and role-scoped projection health endpoints.

    This router must be included before the dynamic `/api/items/{item_id}` route.
    """

    router = APIRouter(tags=["search"])

    @router.get(
        "/api/items/search",
        response_model=ItemSearchResponse,
        operation_id="searchItems",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in ALL_ROLES],
            "x-postgresql-authoritative": True,
        },
    )
    def search_items(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=20, ge=1, le=100),
        authenticated: AuthenticatedUser = Depends(dependencies.roles(*ALL_ROLES)),
    ) -> ItemSearchResponse:
        try:
            result = service.search(
                SearchQuery(text=q, limit=limit),
                SearchPrincipal(user_id=authenticated.user.id),
            )
        except ValueError as exc:
            raise ApiError(422, "invalid_search_query", "Search query is invalid") from exc
        except SearchPermissionDenied as exc:
            raise ApiError(403, "search_forbidden", "Search is not permitted") from exc
        except AuthorityUnavailable as exc:
            raise ApiError(
                503,
                "search_authority_unavailable",
                "Authoritative search is temporarily unavailable",
            ) from exc
        return ItemSearchResponse(
            items=[
                SearchItemResponse(
                    item_id=item.item.item_id,
                    title=item.item.title,
                    likes_snapshot=item.item.likes_snapshot,
                    views_snapshot=item.item.views_snapshot,
                    state_version=item.item.state_version,
                    updated_at=item.item.updated_at,
                    retrieval_source=item.retrieval_source,
                    projection_score=item.projection_score,
                )
                for item in result.items
            ],
            source=result.source,
            degraded=result.degraded,
            projection_index=result.projection_index,
            stale_hits_filtered=result.stale_hits_filtered,
            permission_hits_filtered=result.permission_hits_filtered,
        )

    @router.get(
        "/api/admin/search/health",
        response_model=SearchHealthResponse,
        operation_id="getSearchHealth",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def search_health(
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> SearchHealthResponse:
        report = health_service.report()
        return SearchHealthResponse(
            status=report.status.value,
            projection_reachable=report.projection_reachable,
            fallback_ready=report.fallback_ready,
            alias=report.alias,
            physical_index=report.physical_index,
            reasons=list(report.reasons),
            last_source_watermark=report.last_source_watermark,
        )

    return router
