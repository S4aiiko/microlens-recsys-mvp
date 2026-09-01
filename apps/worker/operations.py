from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.db.base import ensure_utc
from apps.api.app.db.models import (
    OperationBatch,
    OperationBatchStatus,
    PromotionRule,
    PromotionStatus,
)
from apps.api.app.operations.service import OperationService


class ScheduledOperationsRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        service: OperationService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.service = service or OperationService()

    def run_once(self) -> dict[str, int]:
        clock_value = self.clock()
        if clock_value.tzinfo is None or clock_value.utcoffset() is None:
            raise ValueError("scheduled operations clock must be timezone-aware")
        now = clock_value.astimezone(UTC)
        with self.session_factory.begin() as session:
            self._normalize_due_batch_times(session, now=now)
            applied = self.service.apply_due_batches(session, now=now)
            activated, expired = self._reconcile_promotions(session, now=now)
            return {
                "applied_batches": len(applied),
                "activated_promotions": activated,
                "expired_promotions": expired,
            }

    @staticmethod
    def _normalize_due_batch_times(session: Session, *, now: datetime) -> None:
        """Normalize SQLite's naive test datetimes before service-side comparisons.

        PostgreSQL returns timezone-aware values, so this is a no-op in production.
        """

        batches = session.scalars(
            select(OperationBatch).where(
                OperationBatch.status == OperationBatchStatus.SCHEDULED,
                OperationBatch.scheduled_at <= now,
            )
        )
        for batch in batches:
            batch.starts_at = ensure_utc(batch.starts_at)
            batch.scheduled_at = ensure_utc(batch.scheduled_at) if batch.scheduled_at else None
            batch.ends_at = ensure_utc(batch.ends_at) if batch.ends_at else None

    @staticmethod
    def _reconcile_promotions(session: Session, *, now: datetime) -> tuple[int, int]:
        rules = list(
            session.scalars(
                select(PromotionRule)
                .where(
                    PromotionRule.status.in_([PromotionStatus.SCHEDULED, PromotionStatus.ACTIVE])
                )
                .order_by(PromotionRule.id)
                .with_for_update(skip_locked=True)
            )
        )
        activated = 0
        expired = 0
        for rule in rules:
            starts_at = ensure_utc(rule.starts_at)
            ends_at = ensure_utc(rule.ends_at) if rule.ends_at is not None else None
            if ends_at is not None and ends_at <= now:
                rule.status = PromotionStatus.EXPIRED
                expired += 1
            elif rule.status == PromotionStatus.SCHEDULED and starts_at <= now:
                rule.status = PromotionStatus.ACTIVE
                activated += 1
        return activated, expired
