# ruff: noqa: B008
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.service import AuthenticatedUser
from apps.api.app.db.base import ensure_utc
from apps.api.app.db.models import Role

from .domain import AlertOccurrence, AlertStatus
from .schemas import AlertAckResponse, AlertOccurrenceResponse
from .service import SqlAlchemyAlertRepository
from .tables import AlertOccurrenceRow, AlertRuleRow

READ_ROLES = (Role.OPERATOR_READONLY, Role.OPERATOR, Role.ADMIN)
WRITE_ROLES = (Role.OPERATOR, Role.ADMIN)


def build_alerts_router(
    *,
    dependencies: AuthDependencies,
    sessions: sessionmaker[Session],
    repository: SqlAlchemyAlertRepository,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/admin/alerts", tags=["admin", "alerts"])

    @router.get(
        "",
        response_model=list[AlertOccurrenceResponse],
        operation_id="listAlerts",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in READ_ROLES],
        },
    )
    def list_alerts(
        alert_status: AlertStatus | None = Query(default=None, alias="status"),
        limit: Annotated[int, Query(strict=True, ge=1, le=500)] = 100,
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(*READ_ROLES)),
    ) -> list[AlertOccurrenceResponse]:
        statement = (
            select(AlertOccurrenceRow, AlertRuleRow)
            .join(AlertRuleRow, AlertRuleRow.rule_id == AlertOccurrenceRow.rule_id)
            .order_by(AlertOccurrenceRow.fired_at.desc(), AlertOccurrenceRow.occurrence_id)
            .limit(limit)
        )
        if alert_status is not None:
            statement = statement.where(AlertOccurrenceRow.status == alert_status.value)
        with sessions() as session:
            rows = session.execute(statement).all()
        return [_row_response(occurrence, rule) for occurrence, rule in rows]

    @router.post(
        "/{occurrence_id}/ack",
        response_model=AlertAckResponse,
        operation_id="acknowledgeAlert",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in WRITE_ROLES],
        },
    )
    def acknowledge_alert(
        occurrence_id: uuid.UUID,
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(*WRITE_ROLES)),
    ) -> AlertAckResponse:
        before = _occurrence(repository, occurrence_id)
        try:
            occurrence = repository.acknowledge(
                occurrence_id,
                actor=str(authenticated.user.id),
                now=_clock(clock),
            )
        except LookupError as exc:
            raise ApiError(404, "alert_not_found", "Alert occurrence does not exist") from exc
        except ValueError as exc:
            raise ApiError(409, "alert_state_conflict", str(exc)) from exc
        with sessions() as session:
            rule = session.get(AlertRuleRow, occurrence.rule_id)
            if rule is None:
                raise ApiError(409, "alert_rule_missing", "Alert rule no longer exists")
        return AlertAckResponse(
            duplicate=before.status == AlertStatus.ACKNOWLEDGED,
            alert=_domain_response(occurrence, rule),
        )

    return router


def _occurrence(repository: SqlAlchemyAlertRepository, occurrence_id: uuid.UUID) -> AlertOccurrence:
    with repository.session_factory() as session:
        row = session.get(AlertOccurrenceRow, occurrence_id)
        if row is None:
            raise ApiError(404, "alert_not_found", "Alert occurrence does not exist")
        return repository._occurrence_view(row)


def _row_response(occurrence: AlertOccurrenceRow, rule: AlertRuleRow) -> AlertOccurrenceResponse:
    return AlertOccurrenceResponse(
        occurrence_id=occurrence.occurrence_id,
        rule_id=occurrence.rule_id,
        rule_name=rule.name,
        metric_name=rule.metric_name,
        status=occurrence.status,
        observed_value=occurrence.observed_value,
        sample_count=occurrence.sample_count,
        window_start=ensure_utc(occurrence.window_start),
        window_end=ensure_utc(occurrence.window_end),
        fired_at=ensure_utc(occurrence.fired_at),
        version=occurrence.version,
        acknowledged_at=(
            ensure_utc(occurrence.acknowledged_at) if occurrence.acknowledged_at else None
        ),
        acknowledged_by=occurrence.acknowledged_by,
        resolved_at=ensure_utc(occurrence.resolved_at) if occurrence.resolved_at else None,
        resolve_reason=occurrence.resolve_reason,
    )


def _domain_response(occurrence: AlertOccurrence, rule: AlertRuleRow) -> AlertOccurrenceResponse:
    return AlertOccurrenceResponse(
        occurrence_id=occurrence.occurrence_id,
        rule_id=occurrence.rule_id,
        rule_name=rule.name,
        metric_name=rule.metric_name,
        status=occurrence.status.value,
        observed_value=occurrence.observed_value,
        sample_count=occurrence.sample_count,
        window_start=occurrence.window_start,
        window_end=occurrence.window_end,
        fired_at=occurrence.fired_at,
        version=occurrence.version,
        acknowledged_at=occurrence.acknowledged_at,
        acknowledged_by=occurrence.acknowledged_by,
        resolved_at=occurrence.resolved_at,
        resolve_reason=occurrence.resolve_reason,
    )


def _clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("alert API clock must be timezone-aware")
    return value.astimezone(UTC)
