"""Durable asynchronous domain contracts.

The package is intentionally not wired into the application yet. Its tables require
an Alembic migration and its services require integration-owned router/worker wiring.
"""

from .domain import (
    AttemptState,
    Completion,
    DurableClaim,
    DurableJob,
    IdempotencyConflict,
    JobSpec,
    JobState,
    LeaseLost,
    OutboxClaim,
)
from .repository import SqlAlchemyAsyncRepository
from .service import DurableJobService, OutboxHintDispatcher

__all__ = [
    "AttemptState",
    "Completion",
    "DurableClaim",
    "DurableJob",
    "DurableJobService",
    "IdempotencyConflict",
    "JobSpec",
    "JobState",
    "LeaseLost",
    "OutboxClaim",
    "OutboxHintDispatcher",
    "SqlAlchemyAsyncRepository",
]
