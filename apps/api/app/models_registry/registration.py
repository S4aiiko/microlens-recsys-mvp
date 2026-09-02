from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.db.base import ensure_utc
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    ModelStatus,
    ModelVersion,
)

from .repository import ModelRegistryRepository


class RegistrationLoader(Protocol):
    def stage_for_registration(
        self, *, artifact_uri: str, manifest_checksum: str
    ) -> tuple[object, str]: ...


class LoadedBundle(Protocol):
    model_version: str
    data_version: str
    manifest_checksum: str
    config_checksum: str
    manifest: dict[str, Any]
    metrics: dict[str, Any]

    def smoke(self) -> None: ...


class ModelRegistrationConflict(RuntimeError):
    pass


class ModelRegistrationRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RegistrationReceipt:
    model_version: str
    data_version: str
    data_manifest_checksum: str
    config_checksum: str
    manifest_checksum: str
    artifact_checksum: str
    artifact_uri: str
    metrics: dict[str, float]
    trained_at: datetime
    status: ModelStatus
    created: bool


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReadyModelRegistrationService:
    """Validate outside the transaction, then insert an immutable READY row once."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        loader: RegistrationLoader,
        repository: ModelRegistryRepository | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.sessions = sessions
        self.loader = loader
        self.repository = repository or ModelRegistryRepository()
        self.clock = clock

    def register(
        self,
        *,
        artifact_uri: str,
        manifest_checksum: str,
        expected_model_version: str | None = None,
        expected_config_checksum: str | None = None,
        expected_artifact_checksum: str | None = None,
    ) -> RegistrationReceipt:
        if len(manifest_checksum) != 64 or any(
            character not in "0123456789abcdef" for character in manifest_checksum
        ):
            raise ModelRegistrationRejected("manifest_checksum must be a lowercase SHA-256")
        staged, artifact_checksum = self.loader.stage_for_registration(
            artifact_uri=artifact_uri,
            manifest_checksum=manifest_checksum,
        )
        bundle = staged
        identity = self._validated_identity(bundle, manifest_checksum)
        expected = {
            "model_version": (expected_model_version, identity["model_version"]),
            "config_checksum": (expected_config_checksum, identity["config_checksum"]),
            "artifact_checksum": (expected_artifact_checksum, artifact_checksum),
        }
        if any(
            provided is not None and provided != actual for provided, actual in expected.values()
        ):
            raise ModelRegistrationRejected(
                "expected training result does not match the validated ModelBundle"
            )
        event_time = self.clock()
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError("registration clock must be timezone-aware")
        trained_at = event_time.astimezone(UTC)
        with self.sessions.begin() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(731946206)"))
            existing = session.get(ModelVersion, identity["model_version"], with_for_update=True)
            if existing is not None:
                self._require_exact_replay(
                    existing,
                    identity=identity,
                    artifact_uri=artifact_uri,
                    artifact_checksum=artifact_checksum,
                    manifest_checksum=manifest_checksum,
                )
                return self._receipt(existing, created=False)
            model = ModelVersion(
                model_version=identity["model_version"],
                data_version=identity["data_version"],
                data_manifest_checksum=identity["data_manifest_checksum"],
                config_checksum=identity["config_checksum"],
                metrics=identity["metrics"],
                artifact_uri=artifact_uri,
                artifact_checksum=artifact_checksum,
                manifest_checksum=manifest_checksum,
                purpose=identity["purpose"],
                evaluation_comparability=identity["comparability"],
                activation_eligible=True,
                status=ModelStatus.READY,
                failure_reason=None,
                trained_at=trained_at,
                published_at=None,
            )
            self.repository.add_model(session, model)
            return self._receipt(model, created=True)

    @staticmethod
    def _validated_identity(bundle: object, manifest_checksum: str) -> dict[str, Any]:
        required = (
            "model_version",
            "data_version",
            "manifest_checksum",
            "config_checksum",
            "manifest",
            "metrics",
            "smoke",
        )
        if any(not hasattr(bundle, field) for field in required):
            raise ModelRegistrationRejected("staging loader did not return a ModelBundle")
        loaded = bundle  # structural protocol, checked above
        loaded.smoke()
        if not hmac.compare_digest(str(loaded.manifest_checksum), manifest_checksum):
            raise ModelRegistrationRejected("loaded manifest checksum does not match request")
        manifest = loaded.manifest
        if not isinstance(manifest, dict):
            raise ModelRegistrationRejected("model manifest must be an object")
        if (
            manifest.get("status") != "READY"
            or manifest.get("activation_eligible") is not True
            or manifest.get("evaluation_comparability") != Comparability.COMPARABLE.value
            or manifest.get("purpose") == EvaluationPurpose.SYSTEMS_ONLY.value
        ):
            raise ModelRegistrationRejected(
                "READY registration requires a comparable activation-eligible model"
            )
        try:
            purpose = EvaluationPurpose(str(manifest["purpose"]))
        except (KeyError, ValueError) as exc:
            raise ModelRegistrationRejected("model manifest purpose is invalid") from exc
        data_manifest_checksum = manifest.get("data_manifest_checksum")
        if (
            not isinstance(data_manifest_checksum, str)
            or len(data_manifest_checksum) != 64
            or any(character not in "0123456789abcdef" for character in data_manifest_checksum)
        ):
            raise ModelRegistrationRejected("data manifest checksum is invalid")
        evaluation = manifest.get("evaluation")
        metrics = evaluation.get("metrics") if isinstance(evaluation, dict) else None
        if not isinstance(metrics, dict) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            for key, value in metrics.items()
        ):
            raise ModelRegistrationRejected("evaluation metrics are invalid")
        if (
            manifest.get("model_version") != loaded.model_version
            or manifest.get("data_version") != loaded.data_version
            or manifest.get("resolved_config_checksum") != loaded.config_checksum
        ):
            raise ModelRegistrationRejected("bundle identity does not match its manifest")
        return {
            "model_version": str(loaded.model_version),
            "data_version": str(loaded.data_version),
            "data_manifest_checksum": data_manifest_checksum,
            "config_checksum": str(loaded.config_checksum),
            "metrics": {str(key): float(value) for key, value in metrics.items()},
            "purpose": purpose,
            "comparability": Comparability.COMPARABLE,
        }

    @staticmethod
    def _require_exact_replay(
        existing: ModelVersion,
        *,
        identity: dict[str, Any],
        artifact_uri: str,
        artifact_checksum: str,
        manifest_checksum: str,
    ) -> None:
        expected = {
            "data_version": identity["data_version"],
            "data_manifest_checksum": identity["data_manifest_checksum"],
            "config_checksum": identity["config_checksum"],
            "metrics": identity["metrics"],
            "artifact_uri": artifact_uri,
            "artifact_checksum": artifact_checksum,
            "manifest_checksum": manifest_checksum,
            "purpose": identity["purpose"],
            "evaluation_comparability": identity["comparability"],
            "activation_eligible": True,
        }
        if any(getattr(existing, field) != value for field, value in expected.items()) or (
            existing.status not in {ModelStatus.READY, ModelStatus.ACTIVE, ModelStatus.ARCHIVED}
        ):
            raise ModelRegistrationConflict(
                "model_version already exists with different immutable registration data"
            )

    @staticmethod
    def _receipt(model: ModelVersion, *, created: bool) -> RegistrationReceipt:
        if model.data_manifest_checksum is None or model.trained_at is None:
            raise ModelRegistrationConflict("existing model row lacks exact registration lineage")
        return RegistrationReceipt(
            model_version=model.model_version,
            data_version=model.data_version,
            data_manifest_checksum=model.data_manifest_checksum,
            config_checksum=model.config_checksum,
            manifest_checksum=model.manifest_checksum,
            artifact_checksum=model.artifact_checksum,
            artifact_uri=model.artifact_uri,
            metrics=dict(model.metrics),
            trained_at=ensure_utc(model.trained_at),
            status=model.status,
            created=created,
        )
