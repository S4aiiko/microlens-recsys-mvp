from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Select, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.auth.errors import ApiError
from apps.api.app.db.base import ensure_utc, utc_now
from apps.api.app.db.models import (
    FeedType,
    Item,
    OnlineStatus,
    Operation,
    OperationBatch,
    OperationBatchStatus,
    OperationType,
    PromotionRule,
    PromotionStatus,
    Role,
    ScopeType,
    User,
)
from apps.api.app.events.service import fingerprint

from .schemas import OperationBatchRequest, OperationBatchResponse


class OperationFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AuditedOperationFailure(OperationFailure):
    pass


def batch_response(batch: OperationBatch) -> OperationBatchResponse:
    return OperationBatchResponse(
        batch_id=batch.batch_id,
        status=batch.status.value,
        expected_state_version=batch.expected_state_version,
        scheduled_at=ensure_utc(batch.scheduled_at) if batch.scheduled_at else None,
        started_at=ensure_utc(batch.started_at) if batch.started_at else None,
        completed_at=ensure_utc(batch.completed_at) if batch.completed_at else None,
        created_at=ensure_utc(batch.created_at),
        result=batch.result,
    )


class OperationService:
    def create_batch(
        self,
        session: Session,
        *,
        operator_id: uuid.UUID,
        request: OperationBatchRequest,
        now: datetime | None = None,
        before_each_apply: Callable[[str], None] | None = None,
    ) -> OperationBatchResponse:
        event_time = (now or utc_now()).astimezone(UTC)
        request_hash = fingerprint(request.model_dump(mode="python"))
        existing = session.get(OperationBatch, request.batch_id)
        if existing is not None:
            return self._existing_batch(existing, operator_id, request_hash)
        operator_role = self._operator_role(session, operator_id)

        is_scheduled = request.starts_at_utc.astimezone(UTC) > event_time
        batch = OperationBatch(
            batch_id=request.batch_id,
            operator_id=operator_id,
            operator_role=operator_role,
            operation_type=OperationType(request.operation_type),
            targets=request.targets,
            reason=request.reason,
            expected_state_version=0,
            status=(
                OperationBatchStatus.SCHEDULED if is_scheduled else OperationBatchStatus.RUNNING
            ),
            scope_type=ScopeType(request.scope_type),
            scope_value=request.scope_value,
            priority=request.priority,
            target_position=request.target_position,
            starts_at=request.starts_at_utc.astimezone(UTC),
            ends_at=(
                request.ends_at_utc.astimezone(UTC) if request.ends_at_utc is not None else None
            ),
            scheduled_at=(request.starts_at_utc.astimezone(UTC) if is_scheduled else None),
            started_at=None if is_scheduled else event_time,
            completed_at=None,
            created_at=event_time,
            result={"request_hash": request_hash},
        )
        try:
            with session.begin_nested():
                session.add(batch)
                session.flush()
        except IntegrityError:
            existing = session.get(OperationBatch, request.batch_id)
            if existing is None:
                raise
            return self._existing_batch(existing, operator_id, request_hash)

        try:
            with session.begin_nested():
                items = self._preflight(session, request)
                version = max((item.state_version for item in items), default=0)
                batch.expected_state_version = version
                batch.result = {
                    "request_hash": request_hash,
                    "expected_state_versions": {item.id: item.state_version for item in items},
                    "authority_order": ["offline", "natural_filter_and_diversity", "promotion"],
                }
                if not is_scheduled:
                    self._apply(
                        session,
                        batch=batch,
                        items=items,
                        now=event_time,
                        before_each_apply=before_each_apply,
                    )
                session.flush()
                response = batch_response(batch)
        except OperationFailure as exc:
            self._record_failed_batch(
                session,
                batch=batch,
                operator_id=operator_id,
                operator_role=operator_role,
                request=request,
                request_hash=request_hash,
                now=event_time,
                error=exc,
            )
            raise AuditedOperationFailure(exc.code, exc.message) from exc
        except Exception as exc:
            failure = OperationFailure("operation_transaction_failed", type(exc).__name__)
            self._record_failed_batch(
                session,
                batch=batch,
                operator_id=operator_id,
                operator_role=operator_role,
                request=request,
                request_hash=request_hash,
                now=event_time,
                error=failure,
            )
            raise AuditedOperationFailure(failure.code, failure.message) from exc
        return response

    def apply_due_batches(
        self, session: Session, *, now: datetime | None = None
    ) -> list[OperationBatchResponse]:
        event_time = (now or utc_now()).astimezone(UTC)
        batches = list(
            session.scalars(
                select(OperationBatch)
                .where(
                    OperationBatch.status == OperationBatchStatus.SCHEDULED,
                    OperationBatch.scheduled_at <= event_time,
                )
                .order_by(OperationBatch.scheduled_at, OperationBatch.batch_id)
                .with_for_update(skip_locked=True)
            )
        )
        responses: list[OperationBatchResponse] = []
        for batch in batches:
            try:
                with session.begin_nested():
                    items = self._lock_items(session, batch.targets)
                    missing = sorted(set(batch.targets) - {item.id for item in items})
                    if missing:
                        raise OperationFailure(
                            "item_not_found", f"Missing operation targets: {','.join(missing)}"
                        )
                    expected_versions = (batch.result or {}).get("expected_state_versions", {})
                    if any(expected_versions.get(item.id) != item.state_version for item in items):
                        raise OperationFailure(
                            "state_version_conflict",
                            "A target changed after the batch was scheduled",
                        )
                    batch.status = OperationBatchStatus.RUNNING
                    batch.started_at = event_time
                    self._apply(session, batch=batch, items=items, now=event_time)
            except Exception as exc:
                batch.status = OperationBatchStatus.FAILED
                batch.completed_at = event_time
                details = dict(batch.result or {})
                details.update({"error": str(exc), "applied": 0})
                batch.result = details
                for target in batch.targets:
                    session.add(
                        Operation(
                            batch_id=batch.batch_id,
                            target=target,
                            before_value=None,
                            after_value=None,
                            result="failed",
                            error=str(exc),
                            effective_at=event_time,
                        )
                    )
            responses.append(batch_response(batch))
        session.flush()
        return responses

    def active_promotions(
        self,
        session: Session,
        *,
        now: datetime,
        user_id: uuid.UUID | None = None,
        feed_type: FeedType | None = None,
    ) -> list[PromotionRule]:
        event_time = now.astimezone(UTC)
        scopes = [PromotionRule.scope_type == ScopeType.ALL]
        if user_id is not None:
            scopes.append(
                (PromotionRule.scope_type == ScopeType.USER)
                & (PromotionRule.scope_value == str(user_id))
            )
        if feed_type is not None:
            scopes.append(
                (PromotionRule.scope_type == ScopeType.FEED)
                & (PromotionRule.scope_value == feed_type.value)
            )
        query: Select[tuple[PromotionRule]] = (
            select(PromotionRule)
            .join(Item, Item.id == PromotionRule.item_id)
            .where(
                Item.online_status == OnlineStatus.ONLINE,
                PromotionRule.starts_at <= event_time,
                or_(PromotionRule.ends_at.is_(None), PromotionRule.ends_at > event_time),
                or_(*scopes),
            )
            .order_by(
                PromotionRule.priority.desc(),
                PromotionRule.target_position.asc().nulls_last(),
                PromotionRule.id,
            )
        )
        return list(session.scalars(query))

    def _preflight(self, session: Session, request: OperationBatchRequest) -> list[Item]:
        items = self._lock_items(session, request.targets)
        missing = sorted(set(request.targets) - {item.id for item in items})
        if missing:
            raise OperationFailure(
                "item_not_found", f"Missing operation targets: {','.join(missing)}"
            )
        if request.scope_type == "feed" and request.scope_value not in {
            member.value for member in FeedType
        }:
            raise OperationFailure("invalid_feed_scope", "Unknown feed scope")
        if request.scope_type == "user":
            try:
                scoped_user = uuid.UUID(str(request.scope_value))
            except ValueError as exc:
                raise OperationFailure("invalid_user_scope", "User scope must be a UUID") from exc
            if session.get(User, scoped_user) is None:
                raise OperationFailure("invalid_user_scope", "User scope does not exist")
        return items

    def _lock_items(self, session: Session, targets: list[str]) -> list[Item]:
        return list(
            session.scalars(
                select(Item).where(Item.id.in_(targets)).order_by(Item.id).with_for_update()
            )
        )

    def _apply(
        self,
        session: Session,
        *,
        batch: OperationBatch,
        items: list[Item],
        now: datetime,
        before_each_apply: Callable[[str], None] | None = None,
    ) -> None:
        for item in items:
            if before_each_apply is not None:
                before_each_apply(item.id)
            before = {
                "online_status": item.online_status.value,
                "state_version": item.state_version,
            }
            if batch.operation_type == OperationType.OFFLINE:
                item.online_status = OnlineStatus.OFFLINE
                item.state_version += 1
                after = {
                    "online_status": item.online_status.value,
                    "state_version": item.state_version,
                }
            elif batch.operation_type == OperationType.RESTORE:
                item.online_status = OnlineStatus.ONLINE
                item.state_version += 1
                after = {
                    "online_status": item.online_status.value,
                    "state_version": item.state_version,
                }
            else:
                rule = PromotionRule(
                    id=uuid.uuid4(),
                    item_id=item.id,
                    created_by=batch.operator_id,
                    scope_type=batch.scope_type or ScopeType.ALL,
                    scope_value=batch.scope_value,
                    starts_at=batch.starts_at,
                    ends_at=batch.ends_at,
                    priority=batch.priority,
                    target_position=batch.target_position,
                    reason=batch.reason,
                    status=(
                        PromotionStatus.ACTIVE
                        if batch.starts_at <= now and (batch.ends_at is None or now < batch.ends_at)
                        else PromotionStatus.EXPIRED
                    ),
                    operation_batch_id=batch.batch_id,
                )
                session.add(rule)
                after = {"promotion_rule_id": str(rule.id), "online_authority_preserved": True}
            item.updated_at = now
            session.add(
                Operation(
                    batch_id=batch.batch_id,
                    target=item.id,
                    before_value=before,
                    after_value=after,
                    result="succeeded",
                    error=None,
                    effective_at=now,
                )
            )
        batch.status = OperationBatchStatus.SUCCEEDED
        batch.completed_at = now
        result = dict(batch.result or {})
        result.update({"applied": len(items), "failed": 0})
        batch.result = result

    def _record_failed_batch(
        self,
        session: Session,
        *,
        batch: OperationBatch,
        operator_id: uuid.UUID,
        operator_role: Role,
        request: OperationBatchRequest,
        request_hash: str,
        now: datetime,
        error: OperationFailure,
    ) -> None:
        batch = session.get(OperationBatch, batch.batch_id) or batch
        batch.operator_id = operator_id
        batch.operator_role = operator_role
        batch.status = OperationBatchStatus.FAILED
        batch.scheduled_at = None
        batch.started_at = batch.started_at or now
        batch.completed_at = now
        batch.result = {
            "request_hash": request_hash,
            "error_code": error.code,
            "error": error.message,
            "applied": 0,
        }
        session.flush()
        session.add_all(
            [
                Operation(
                    batch_id=batch.batch_id,
                    target=target,
                    before_value=None,
                    after_value=None,
                    result="failed",
                    error=error.message,
                    effective_at=now,
                )
                for target in request.targets
            ]
        )
        session.flush()

    @staticmethod
    def _existing_batch(
        existing: OperationBatch, operator_id: uuid.UUID, request_hash: str
    ) -> OperationBatchResponse:
        if (
            existing.operator_id != operator_id
            or not existing.result
            or existing.result.get("request_hash") != request_hash
        ):
            raise ApiError(409, "batch_id_conflict", "batch_id has different content")
        if existing.status == OperationBatchStatus.FAILED:
            raise AuditedOperationFailure(
                str(existing.result.get("error_code") or "operation_failed"),
                str(existing.result.get("error") or "Operation batch failed"),
            )
        return batch_response(existing)

    @staticmethod
    def _operator_role(session: Session, operator_id: uuid.UUID) -> Role:
        role = session.scalar(select(User.role).where(User.id == operator_id))
        if role not in {Role.OPERATOR, Role.ADMIN}:
            raise ApiError(403, "insufficient_role", "Operator or admin role is required")
        return Role(role)
