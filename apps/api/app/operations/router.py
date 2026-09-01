# ruff: noqa: B008
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.models import (
    Item,
    ModelStatus,
    ModelVersion,
    OnlineStatus,
    Operation,
    OperationBatch,
    Role,
)

from .schemas import (
    AdminItemResponse,
    AuditOperationResponse,
    ItemDetailResponse,
    OperationBatchRequest,
    OperationBatchResponse,
)
from .service import AuditedOperationFailure, OperationService

# FastAPI dependency markers are intentionally evaluated in route signatures.

READ_ROLES = (Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)
WRITE_ROLES = (Role.OPERATOR, Role.ADMIN)


def build_operations_router(
    *,
    get_session: Callable[..., Session],
    dependencies: AuthDependencies,
    service: OperationService,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get(
        "/items",
        response_model=list[AdminItemResponse],
        operation_id="searchAdminItems",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def search_items(
        query: str | None = Query(default=None, max_length=200),
        online_status: OnlineStatus | None = None,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> list[AdminItemResponse]:
        statement = select(Item)
        if query:
            pattern = f"%{query}%"
            statement = statement.where(or_(Item.id.ilike(pattern), Item.title.ilike(pattern)))
        if online_status is not None:
            statement = statement.where(Item.online_status == online_status)
        items = session.scalars(statement.order_by(Item.id).limit(200)).all()
        return [
            AdminItemResponse(
                item_id=item.id,
                title=item.title,
                heat=max(0, int(item.likes_snapshot or 0) + int(item.views_snapshot or 0)),
                online_status=item.online_status.value,
                updated_at=item.updated_at,
                state_version=item.state_version,
                cover=item.cover_ref,
            )
            for item in items
        ]

    @router.post(
        "/promotions",
        response_model=OperationBatchResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="createPromotion",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def create_promotion(
        payload: OperationBatchRequest,
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> OperationBatchResponse:
        if payload.operation_type != "promote":
            raise ApiError(422, "operation_type_mismatch", "Promotion endpoint requires promote")
        return _create_and_commit(session, service, authenticated, payload)

    @router.post(
        "/operation-batches",
        response_model=OperationBatchResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="createOperationBatch",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def create_operation_batch(
        payload: OperationBatchRequest,
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> OperationBatchResponse:
        return _create_and_commit(session, service, authenticated, payload)

    @router.get(
        "/operations",
        response_model=list[AuditOperationResponse],
        operation_id="listOperations",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def list_operations(
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> list[AuditOperationResponse]:
        operations = session.execute(
            select(Operation, OperationBatch)
            .join(OperationBatch, OperationBatch.batch_id == Operation.batch_id)
            .order_by(Operation.effective_at.desc(), Operation.id)
            .limit(500)
        ).all()
        return [
            AuditOperationResponse(
                operation_id=operation.id,
                batch_id=operation.batch_id,
                operator_id=batch.operator_id,
                operator_role=batch.operator_role.value,
                operation_type=batch.operation_type.value,
                reason=batch.reason,
                targets=list(batch.targets),
                target=operation.target,
                before_value=operation.before_value,
                after_value=operation.after_value,
                result=operation.result,
                error=operation.error,
                effective_at=operation.effective_at,
            )
            for operation, batch in operations
        ]

    return router


def build_items_router(
    *, get_session: Callable[..., Session], dependencies: AuthDependencies
) -> APIRouter:
    router = APIRouter(tags=["feeds"])

    @router.get(
        "/api/items/{item_id}",
        response_model=ItemDetailResponse,
        operation_id="getItem",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in Role],
        },
    )
    def get_item(
        item_id: str,
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(
            dependencies.roles(Role.USER, Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)
        ),
    ) -> ItemDetailResponse:
        item = session.get(Item, item_id)
        if item is None or item.online_status != OnlineStatus.ONLINE:
            raise ApiError(404, "item_not_found", "Online item does not exist")
        active_model = session.scalar(
            select(ModelVersion.model_version).where(ModelVersion.status == ModelStatus.ACTIVE)
        )
        return ItemDetailResponse(
            item_id=item.id,
            title=item.title,
            cover=item.cover_ref,
            position=0,
            source="item_detail",
            score=0.0,
            reason="item_detail",
            model_version=active_model or "n/a",
        )

    return router


def _create_and_commit(
    session: Session,
    service: OperationService,
    authenticated: AuthenticatedUser,
    payload: OperationBatchRequest,
) -> OperationBatchResponse:
    try:
        response = service.create_batch(session, operator_id=authenticated.user.id, request=payload)
    except AuditedOperationFailure as exc:
        session.commit()
        raise ApiError(422, exc.code, exc.message) from exc
    session.commit()
    return response
