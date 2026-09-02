from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apps.api.app.auth.errors import ApiError
from apps.api.app.db.models import (
    Comparability,
    ModelActivationAttempt,
    ModelStatus,
    ModelVersion,
)


class StagingLoader(Protocol):
    def stage(
        self, *, artifact_uri: str, artifact_checksum: str, manifest_checksum: str
    ) -> object: ...


class RuntimeModelSlot(Protocol):
    def swap(self, *, model_version: str, staged_bundle: object) -> None: ...


@dataclass(frozen=True)
class FileStagingLoader:
    artifact_root: Path
    load_bundle: Callable[[Path, str], object]

    def stage(self, *, artifact_uri: str, artifact_checksum: str, manifest_checksum: str) -> object:
        root = self.artifact_root.resolve()
        artifact = (root / artifact_uri).resolve()
        if root not in artifact.parents:
            raise ValueError("artifact path escapes the configured artifact root")
        if not artifact.is_file():
            raise ValueError("artifact does not exist")
        digest = hashlib.sha256()
        with artifact.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), artifact_checksum):
            raise ValueError("artifact checksum mismatch")
        return self.load_bundle(artifact, manifest_checksum)


@dataclass(frozen=True)
class PreparedActivation:
    model_version: str
    manifest_checksum: str
    staged_bundle: object


@dataclass(frozen=True)
class ActivationPlan:
    model: ModelVersion
    staged_bundle: object


class ActivationService:
    def __init__(
        self,
        *,
        publish_token: str,
        loader: StagingLoader,
        resource_integrator: Callable[[Session, object], object] | None = None,
    ) -> None:
        if len(publish_token.encode("utf-8")) < 24:
            raise ValueError("publish token must contain at least 24 bytes")
        self._publish_token = publish_token
        self.loader = loader
        self.resource_integrator = resource_integrator

    def authenticate_publish_token(self, provided: str | None) -> None:
        if provided is None or not hmac.compare_digest(provided, self._publish_token):
            raise ApiError(401, "invalid_publish_token", "Publish token is invalid")

    def begin_attempt(
        self,
        session: Session,
        *,
        version: str,
        expected_current_version: str | None,
        now: datetime | None = None,
    ) -> ModelActivationAttempt:
        if session.get(ModelVersion, version) is None:
            raise ApiError(422, "model_not_found", "Model version does not exist")
        attempt = ModelActivationAttempt(
            model_version=version,
            expected_current_version=expected_current_version,
            status="started",
            created_at=(now or datetime.now(UTC)).astimezone(UTC),
        )
        session.add(attempt)
        session.flush()
        return attempt

    def record_failure(
        self,
        session: Session,
        *,
        attempt_id: uuid.UUID,
        code: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        attempt = session.get(ModelActivationAttempt, attempt_id, with_for_update=True)
        if attempt is None:
            raise LookupError("activation attempt does not exist")
        event_time = (now or datetime.now(UTC)).astimezone(UTC)
        attempt.status = "failed"
        attempt.failure_code = code
        attempt.failure_reason = reason
        attempt.completed_at = event_time
        model = session.get(ModelVersion, attempt.model_version, with_for_update=True)
        if model is not None:
            model.failure_reason = f"{code}: {reason}"
        session.flush()

    def record_success(
        self,
        session: Session,
        *,
        attempt_id: uuid.UUID,
        now: datetime | None = None,
    ) -> None:
        attempt = session.get(ModelActivationAttempt, attempt_id, with_for_update=True)
        if attempt is None:
            raise LookupError("activation attempt does not exist")
        attempt.status = "succeeded"
        attempt.failure_code = None
        attempt.failure_reason = None
        attempt.completed_at = (now or datetime.now(UTC)).astimezone(UTC)
        session.flush()

    def prepare(
        self,
        session: Session,
        *,
        version: str,
        manifest_checksum: str,
    ) -> PreparedActivation:
        model = session.get(ModelVersion, version)
        if model is None:
            raise ApiError(422, "model_not_found", "Model version does not exist")
        self._validate_target(model, manifest_checksum)
        try:
            stage_activation = getattr(self.loader, "stage_activation", None)
            if callable(stage_activation):
                if model.data_manifest_checksum is None:
                    raise ValueError("active model is missing its processed-data checksum")
                staged = stage_activation(
                    model_version=model.model_version,
                    data_version=model.data_version,
                    data_manifest_checksum=model.data_manifest_checksum,
                    artifact_uri=model.artifact_uri,
                    artifact_checksum=model.artifact_checksum,
                    manifest_checksum=manifest_checksum,
                )
            else:
                staged = self.loader.stage(
                    artifact_uri=model.artifact_uri,
                    artifact_checksum=model.artifact_checksum,
                    manifest_checksum=manifest_checksum,
                )
        except Exception as exc:
            raise ApiError(422, "staging_load_failed", "Model staging validation failed") from exc
        return PreparedActivation(
            model_version=version,
            manifest_checksum=manifest_checksum,
            staged_bundle=staged,
        )

    def activate_prepared(
        self,
        session: Session,
        *,
        prepared: PreparedActivation,
        expected_current_version: str | None,
        attempt_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> ActivationPlan:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(731946205)"))
        target = session.scalar(
            select(ModelVersion)
            .where(ModelVersion.model_version == prepared.model_version)
            .with_for_update()
        )
        if target is None:
            raise ApiError(422, "model_not_found", "Model version does not exist")
        self._validate_target(target, prepared.manifest_checksum)
        active = session.scalar(
            select(ModelVersion).where(ModelVersion.status == ModelStatus.ACTIVE).with_for_update()
        )
        current = active.model_version if active is not None else None
        if current != expected_current_version:
            raise ApiError(
                409,
                "activation_cas_conflict",
                "Current active model does not match expected_current_version",
                details={"expected": expected_current_version, "actual": current},
            )
        if self.resource_integrator is not None:
            try:
                self.resource_integrator(session, prepared.staged_bundle)
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError(
                    422,
                    "serving_resource_integration_failed",
                    "Serving resource integration failed",
                ) from exc
        event_time = (now or datetime.now(UTC)).astimezone(UTC)
        if active is not None and active.model_version != target.model_version:
            active.status = ModelStatus.ARCHIVED
            # PostgreSQL enforces a partial unique index for ACTIVE. Flush the
            # archive first so ORM update ordering can never create two ACTIVE
            # rows, while both writes remain inside the same transaction.
            session.flush([active])
        target.status = ModelStatus.ACTIVE
        target.published_at = event_time
        target.failure_reason = None
        if attempt_id is not None:
            self.record_success(session, attempt_id=attempt_id, now=event_time)
        session.flush()
        return ActivationPlan(model=target, staged_bundle=prepared.staged_bundle)

    @staticmethod
    def _validate_target(model: ModelVersion, manifest_checksum: str) -> None:
        if not hmac.compare_digest(model.manifest_checksum, manifest_checksum):
            raise ApiError(422, "manifest_checksum_mismatch", "Manifest checksum does not match")
        if model.status != ModelStatus.READY:
            raise ApiError(422, "model_not_ready", "Only READY models can be activated")
        if (
            not model.activation_eligible
            or model.evaluation_comparability != Comparability.COMPARABLE
        ):
            raise ApiError(
                422,
                "model_not_activation_eligible",
                "Model is not comparable and activation eligible",
            )
