from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from apps.api.app.async_runtime.domain import canonical_json, require_aware


class OperationKind(StrEnum):
    PROMOTE = "promote"
    OFFLINE = "offline"
    RESTORE = "restore"


class ExpectedStateConflict(RuntimeError):
    """At least one target changed since the all-or-none batch was scheduled."""


@dataclass(frozen=True)
class TargetExpectation:
    target_id: str
    state_version: int

    def __post_init__(self) -> None:
        if not self.target_id or len(self.target_id) > 255:
            raise ValueError("target_id must contain 1..255 characters")
        if isinstance(self.state_version, bool) or not isinstance(self.state_version, int):
            raise ValueError("state_version must be an integer")
        if self.state_version < 0:
            raise ValueError("state_version must be nonnegative")


@dataclass(frozen=True)
class OperationJobSpec:
    operation_id: uuid.UUID
    idempotency_key: str
    kind: OperationKind
    targets: tuple[TargetExpectation, ...]
    due_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 220:
            raise ValueError("idempotency_key must contain 1..220 characters")
        if not 1 <= len(self.targets) <= 100:
            raise ValueError("operation batch must contain 1..100 targets")
        target_ids = [target.target_id for target in self.targets]
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("operation targets must be unique")
        require_aware(self.due_at, field="due_at")
        canonical_json(self.payload)
        if self.max_attempts < 1 or self.max_attempts > 100:
            raise ValueError("max_attempts must be between 1 and 100")


@dataclass(frozen=True)
class OperationBatchResult:
    operation_id: uuid.UUID
    applied_targets: tuple[str, ...]
    state_versions: dict[str, int]
    duplicate: bool = False

    def __post_init__(self) -> None:
        if set(self.applied_targets) != set(self.state_versions):
            raise ValueError("operation result must include one version per applied target")
        if any(
            isinstance(version, bool) or not isinstance(version, int) or version < 0
            for version in self.state_versions.values()
        ):
            raise ValueError("result state versions must be nonnegative integers")
