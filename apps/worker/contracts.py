from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from apps.api.app.db.models import Comparability, EvaluationPurpose


class Broker(Protocol):
    """Best-effort notification channel; PostgreSQL remains the queue authority."""

    def notify(self, job_id: uuid.UUID) -> None: ...

    def receive(self, *, timeout_seconds: int = 0) -> uuid.UUID | None: ...

    def ping(self) -> bool: ...


@dataclass(frozen=True)
class TrainingRequest:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt: int
    data_version: str
    data_manifest_checksum: str
    config_checksum: str
    purpose: EvaluationPurpose
    evaluation_comparability: Comparability
    activation_eligible: bool


@dataclass(frozen=True)
class TrainingControl:
    heartbeat: Callable[[], None]
    cancellation_requested: Callable[[], bool]


class TrainingHandler(Protocol):
    def __call__(self, request: TrainingRequest, control: TrainingControl) -> dict[str, Any]: ...


class RetryableTrainingError(RuntimeError):
    pass


class PermanentTrainingError(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class CancellationRequested(RuntimeError):
    pass
