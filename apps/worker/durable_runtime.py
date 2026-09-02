from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.alerts.service import AlertService
from apps.api.app.async_runtime.domain import (
    DurableClaim,
    payload_fingerprint,
    require_aware,
)
from apps.api.app.async_runtime.runtime import AsyncRuntime, create_async_runtime
from apps.api.app.async_runtime.service import OutboxHintDispatcher
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
from apps.api.app.operation_jobs.domain import (
    ExpectedStateConflict,
    OperationBatchResult,
    OperationKind,
    TargetExpectation,
)
from apps.api.app.operation_jobs.service import OperationTaskHandler
from apps.worker.async_tasks import RunOnceScheduler, RunOnceWorker, TaskHandler
from apps.worker.scheduler import AlertEvaluationScheduler, ProductionScheduler


class AuthorizedTaskHandler:
    """Unwrap an API-authorized task without consulting mutable user permissions."""

    def __init__(self, handler: TaskHandler) -> None:
        self.handler = handler
        self.task_name = handler.task_name

    def handle(self, claim: DurableClaim, *, now: datetime) -> dict[str, object]:
        payload = claim.job.payload
        if set(payload) != {"schema_version", "authorized_submission", "task_payload"}:
            raise ValueError("authorized task envelope has unexpected fields")
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported authorized task envelope")
        submission = payload.get("authorized_submission")
        task_payload = payload.get("task_payload")
        if not isinstance(submission, dict) or not isinstance(task_payload, dict):
            raise ValueError("authorized task envelope is malformed")
        if set(submission) != {"actor_id", "actor_role", "authorized_at"}:
            raise ValueError("authorized submission has unexpected fields")
        uuid.UUID(_required_string(submission, "actor_id"))
        # This validates persisted integrity only. It deliberately does not query the
        # user's current role: authorization happened before the durable insert.
        if _required_string(submission, "actor_role") not in {
            Role.OPERATOR.value,
            Role.ADMIN.value,
        }:
            raise ValueError("persisted submission role is invalid")
        _parse_aware(_required_string(submission, "authorized_at"), "authorized_at")
        unwrapped = dataclasses.replace(
            claim,
            job=dataclasses.replace(claim.job, payload=task_payload),
        )
        return self.handler.handle(unwrapped, now=now)


class SqlAlchemyAtomicOperationExecutor:
    """Apply every operation target and its durable receipt in one DB transaction."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def apply_all(
        self,
        *,
        operation_id: uuid.UUID,
        kind: OperationKind,
        targets: tuple[TargetExpectation, ...],
        payload: dict[str, Any],
        now: datetime,
    ) -> OperationBatchResult:
        event_time = require_aware(now, field="now")
        parsed = _OperationPayload.parse(payload)
        request_fingerprint = payload_fingerprint(
            {
                "operation_id": str(operation_id),
                "kind": kind.value,
                "targets": [
                    {"target_id": target.target_id, "state_version": target.state_version}
                    for target in targets
                ],
                "payload": payload,
            }
        )
        with self.sessions.begin() as session:
            existing = session.get(OperationBatch, operation_id, with_for_update=True)
            if existing is not None:
                return self._existing_result(
                    existing,
                    operation_id=operation_id,
                    request_fingerprint=request_fingerprint,
                )

            items = list(
                session.scalars(
                    select(Item)
                    .where(Item.id.in_([target.target_id for target in targets]))
                    .order_by(Item.id)
                    .with_for_update()
                )
            )
            by_id = {item.id: item for item in items}
            if set(by_id) != {target.target_id for target in targets}:
                raise ExpectedStateConflict("one or more operation targets do not exist")
            for expected in sorted(targets, key=lambda target: target.target_id):
                if by_id[expected.target_id].state_version != expected.state_version:
                    raise ExpectedStateConflict(
                        f"state version changed for target {expected.target_id}"
                    )
            self._validate_scope(session, parsed)

            batch = OperationBatch(
                batch_id=operation_id,
                operator_id=parsed.actor_id,
                operator_role=parsed.actor_role,
                operation_type=OperationType(kind.value),
                targets=[target.target_id for target in targets],
                reason=parsed.reason,
                expected_state_version=max(target.state_version for target in targets),
                status=OperationBatchStatus.RUNNING,
                scope_type=parsed.scope_type,
                scope_value=parsed.scope_value,
                priority=parsed.priority,
                target_position=parsed.target_position,
                starts_at=parsed.starts_at,
                ends_at=parsed.ends_at,
                scheduled_at=parsed.starts_at,
                started_at=event_time,
                completed_at=None,
                created_at=parsed.authorized_at,
                result={
                    "async_request_fingerprint": request_fingerprint,
                    "expected_state_versions": {
                        target.target_id: target.state_version for target in targets
                    },
                    "authority_order": [
                        "offline",
                        "natural_filter_and_diversity",
                        "promotion",
                    ],
                },
            )
            session.add(batch)
            session.flush()

            versions: dict[str, int] = {}
            for expected in sorted(targets, key=lambda target: target.target_id):
                item = by_id[expected.target_id]
                before = {
                    "online_status": item.online_status.value,
                    "state_version": item.state_version,
                }
                if kind == OperationKind.OFFLINE:
                    item.online_status = OnlineStatus.OFFLINE
                    item.state_version += 1
                    after: dict[str, Any] = {
                        "online_status": item.online_status.value,
                        "state_version": item.state_version,
                    }
                elif kind == OperationKind.RESTORE:
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
                        created_by=parsed.actor_id,
                        scope_type=parsed.scope_type,
                        scope_value=parsed.scope_value,
                        starts_at=parsed.starts_at,
                        ends_at=parsed.ends_at,
                        priority=parsed.priority,
                        target_position=parsed.target_position,
                        reason=parsed.reason,
                        status=(
                            PromotionStatus.ACTIVE
                            if parsed.ends_at is None or event_time < parsed.ends_at
                            else PromotionStatus.EXPIRED
                        ),
                        operation_batch_id=operation_id,
                    )
                    session.add(rule)
                    after = {
                        "promotion_rule_id": str(rule.id),
                        "online_authority_preserved": True,
                    }
                item.updated_at = event_time
                versions[item.id] = item.state_version
                session.add(
                    Operation(
                        batch_id=operation_id,
                        target=item.id,
                        before_value=before,
                        after_value=after,
                        result="succeeded",
                        error=None,
                        effective_at=event_time,
                    )
                )

            batch.status = OperationBatchStatus.SUCCEEDED
            batch.completed_at = event_time
            batch.result = {
                **dict(batch.result or {}),
                "applied": len(targets),
                "failed": 0,
                "applied_targets": sorted(versions),
                "state_versions": versions,
            }
            session.flush()
            return OperationBatchResult(
                operation_id=operation_id,
                applied_targets=tuple(sorted(versions)),
                state_versions=versions,
                duplicate=False,
            )

    @staticmethod
    def _validate_scope(session: Session, parsed: _OperationPayload) -> None:
        if parsed.scope_type == ScopeType.FEED and parsed.scope_value not in {
            member.value for member in FeedType
        }:
            raise ValueError("unknown feed scope")
        if parsed.scope_type == ScopeType.USER:
            try:
                user_id = uuid.UUID(str(parsed.scope_value))
            except ValueError as exc:
                raise ValueError("user scope must be a UUID") from exc
            if session.get(User, user_id) is None:
                raise ValueError("user scope does not exist")

    @staticmethod
    def _existing_result(
        batch: OperationBatch,
        *,
        operation_id: uuid.UUID,
        request_fingerprint: str,
    ) -> OperationBatchResult:
        result = batch.result or {}
        if result.get("async_request_fingerprint") != request_fingerprint:
            raise ExpectedStateConflict("operation receipt has different immutable input")
        if batch.status != OperationBatchStatus.SUCCEEDED:
            raise ExpectedStateConflict("operation receipt is not successful")
        raw_targets = result.get("applied_targets")
        raw_versions = result.get("state_versions")
        if not isinstance(raw_targets, list) or not isinstance(raw_versions, dict):
            raise ValueError("operation receipt is malformed")
        if any(not isinstance(value, str) for value in raw_targets):
            raise ValueError("operation receipt targets are malformed")
        versions: dict[str, int] = {}
        for key, value in raw_versions.items():
            if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("operation receipt versions are malformed")
            versions[key] = value
        return OperationBatchResult(
            operation_id=operation_id,
            applied_targets=tuple(raw_targets),
            state_versions=versions,
            duplicate=True,
        )


@dataclass(frozen=True)
class DurableWorkerRuntime:
    async_runtime: AsyncRuntime
    scheduler: ProductionScheduler

    def run_once(self) -> dict[str, Any]:
        result = self.scheduler.run_once()
        sink = self.async_runtime.hint_sink
        redis_ready = False
        if sink is not None:
            try:
                redis_ready = sink.ping()
            except Exception:
                redis_ready = False
        return {
            **result,
            "database_authoritative": True,
            "redis_ready": redis_ready,
            "redis_degraded": not redis_ready,
        }


def build_durable_worker_runtime(
    sessions: sessionmaker[Session],
    *,
    handlers: Sequence[TaskHandler] = (),
    worker_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    redis_url: str | None = None,
    redis_client: Any | None = None,
    alert_service: AlertService | None = None,
    lease_seconds: int = 60,
    retry_delay_seconds: int = 5,
) -> DurableWorkerRuntime:
    if not worker_id or len(worker_id) > 255:
        raise ValueError("worker_id must contain 1..255 characters")
    runtime = create_async_runtime(
        sessions,
        redis_url=redis_url,
        redis_client=redis_client,
        lease_seconds=lease_seconds,
    )
    wrapped_handlers: list[TaskHandler] = [AuthorizedTaskHandler(handler) for handler in handlers]
    wrapped_handlers.append(OperationTaskHandler(SqlAlchemyAtomicOperationExecutor(sessions)))
    worker = RunOnceWorker(
        runtime.jobs,
        wrapped_handlers,
        worker_id=worker_id,
        clock=clock,
        retry_delay_seconds=retry_delay_seconds,
    )
    outbox = (
        OutboxHintDispatcher(runtime.repository, runtime.hint_sink)
        if runtime.hint_sink is not None
        else None
    )
    durable = RunOnceScheduler(worker, outbox=outbox, clock=clock)
    alerts = (
        AlertEvaluationScheduler(sessions, alert_service, clock=clock)
        if alert_service is not None
        else None
    )
    return DurableWorkerRuntime(
        async_runtime=runtime,
        scheduler=ProductionScheduler(durable, alerts=alerts, clock=clock),
    )


@dataclass(frozen=True)
class _OperationPayload:
    actor_id: uuid.UUID
    actor_role: Role
    authorized_at: datetime
    starts_at: datetime
    ends_at: datetime | None
    scope_type: ScopeType
    scope_value: str | None
    priority: int
    target_position: int | None
    reason: str

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> _OperationPayload:
        expected = {
            "authorized_submission",
            "scope_type",
            "scope_value",
            "starts_at_utc",
            "ends_at_utc",
            "priority",
            "target_position",
            "reason",
        }
        if set(payload) != expected:
            raise ValueError("operation payload has unexpected fields")
        submission = payload["authorized_submission"]
        if not isinstance(submission, dict) or set(submission) != {
            "actor_id",
            "actor_role",
            "authorized_at",
        }:
            raise ValueError("authorized operation submission is malformed")
        actor_role_value = _required_string(submission, "actor_role")
        if actor_role_value not in {Role.OPERATOR.value, Role.ADMIN.value}:
            raise ValueError("persisted operation role is invalid")
        scope_type = ScopeType(_required_string(payload, "scope_type"))
        scope_value = payload["scope_value"]
        if scope_value is not None and not isinstance(scope_value, str):
            raise ValueError("scope_value must be a string or null")
        if (scope_type == ScopeType.ALL) != (scope_value is None):
            raise ValueError("operation scope shape is invalid")
        priority = _strict_integer(payload, "priority", minimum=0)
        target_position = payload["target_position"]
        if target_position is not None:
            target_position = _strict_integer(payload, "target_position", minimum=0)
        reason = _required_string(payload, "reason")
        if len(reason) > 500:
            raise ValueError("operation reason is too long")
        starts_at = _parse_aware(_required_string(payload, "starts_at_utc"), "starts_at_utc")
        raw_ends = payload["ends_at_utc"]
        ends_at = None
        if raw_ends is not None:
            if not isinstance(raw_ends, str):
                raise ValueError("ends_at_utc must be a string or null")
            ends_at = _parse_aware(raw_ends, "ends_at_utc")
            if ends_at <= starts_at:
                raise ValueError("ends_at_utc must be after starts_at_utc")
        return cls(
            actor_id=uuid.UUID(_required_string(submission, "actor_id")),
            actor_role=Role(actor_role_value),
            authorized_at=_parse_aware(
                _required_string(submission, "authorized_at"), "authorized_at"
            ),
            starts_at=starts_at,
            ends_at=ends_at,
            scope_type=scope_type,
            scope_value=scope_value,
            priority=priority,
            target_position=target_position,
            reason=reason,
        )


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _strict_integer(payload: dict[str, Any], field: str, *, minimum: int) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _parse_aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    return require_aware(parsed, field=field)
