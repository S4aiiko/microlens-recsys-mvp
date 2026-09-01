from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LEASE_EXPIRED = "lease_expired"
    CANCELLED = "cancelled"


class OutboxState(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    PUBLISHED = "published"


class IdempotencyConflict(RuntimeError):
    """The same idempotency key was used for different immutable input."""


class LeaseLost(RuntimeError):
    """A worker tried to mutate a job without the current fencing token."""


@dataclass(frozen=True)
class JobSpec:
    idempotency_key: str
    task_name: str
    payload: dict[str, Any]
    due_at: datetime
    max_attempts: int = 3
    job_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 255:
            raise ValueError("idempotency_key must contain 1..255 characters")
        if not self.task_name or len(self.task_name) > 128:
            raise ValueError("task_name must contain 1..128 characters")
        if self.max_attempts < 1 or self.max_attempts > 100:
            raise ValueError("max_attempts must be between 1 and 100")
        require_aware(self.due_at, field="due_at")
        canonical_json(self.payload)

    @property
    def fingerprint(self) -> str:
        return payload_fingerprint(
            {
                "task_name": self.task_name,
                "payload": self.payload,
                "due_at": self.due_at.astimezone(UTC).isoformat(),
                "max_attempts": self.max_attempts,
            }
        )


@dataclass(frozen=True)
class DurableJob:
    job_id: uuid.UUID
    idempotency_key: str
    task_name: str
    payload: dict[str, Any]
    payload_fingerprint: str
    state: JobState
    due_at: datetime
    max_attempts: int
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    last_error: str | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class DurableClaim:
    job: DurableJob
    attempt_id: uuid.UUID
    attempt: int
    worker_id: str
    fence_token: uuid.UUID
    lease_expires_at: datetime


@dataclass(frozen=True)
class Completion:
    state: JobState
    duplicate: bool


@dataclass(frozen=True)
class OutboxClaim:
    outbox_id: int
    topic: str
    payload: dict[str, Any]
    delivery_token: uuid.UUID
    delivery_attempt: int
    lease_expires_at: datetime


class HintSink(Protocol):
    """A reconstructable, non-authoritative delivery surface such as Redis."""

    def notify(self, topic: str, payload: dict[str, Any]) -> None: ...


def payload_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be canonical JSON data") from exc


def require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def safe_error(error: BaseException) -> str:
    message = f"{type(error).__name__}: {error}"
    message = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", message)
    message = re.sub(
        r"(?i)(password|token|secret|authorization|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        message,
    )
    message = re.sub(r"://[^:@/\s]+:[^@/\s]+@", "://[REDACTED]@", message)
    return message[:2000]
