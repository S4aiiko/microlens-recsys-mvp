from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    ModelStatus,
    ModelVersion,
)
from apps.api.app.models_registry.registration import (
    ModelRegistrationConflict,
    ModelRegistrationRejected,
    ReadyModelRegistrationService,
)
from apps.worker import model_training
from tests.api._support import factory_for, sqlite_engine

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass
class FakeBundle:
    model_version: str = "model-ready-v1"
    data_version: str = "data-v1"
    manifest_checksum: str = "c" * 64
    config_checksum: str = "b" * 64
    manifest: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    smoke_calls: int = 0

    def __post_init__(self) -> None:
        if self.metrics is None:
            self.metrics = {"two_stage": {"recall@20": 0.25}}
        if self.manifest is None:
            self.manifest = {
                "model_version": self.model_version,
                "data_version": self.data_version,
                "data_manifest_checksum": "a" * 64,
                "resolved_config_checksum": self.config_checksum,
                "purpose": "base_official",
                "evaluation_comparability": "comparable",
                "activation_eligible": True,
                "status": "READY",
                "evaluation": {"metrics": {"two_stage.recall@20": 0.25}},
            }

    def smoke(self) -> None:
        self.smoke_calls += 1


@dataclass
class FakeLoader:
    bundle: object
    artifact_checksum: str = "d" * 64

    def stage_for_registration(
        self, *, artifact_uri: str, manifest_checksum: str
    ) -> tuple[object, str]:
        if artifact_uri != "model-ready-v1/bundle.json":
            raise ValueError("unexpected artifact")
        if manifest_checksum != "c" * 64:
            raise ValueError("unexpected manifest checksum")
        return self.bundle, self.artifact_checksum


def active_model() -> ModelVersion:
    return ModelVersion(
        model_version="active-v0",
        data_version="data-v0",
        data_manifest_checksum="9" * 64,
        config_checksum="8" * 64,
        metrics={},
        artifact_uri="active-v0/bundle.json",
        artifact_checksum="7" * 64,
        manifest_checksum="6" * 64,
        purpose=EvaluationPurpose.BASE_OFFICIAL,
        evaluation_comparability=Comparability.COMPARABLE,
        activation_eligible=True,
        status=ModelStatus.ACTIVE,
        trained_at=NOW,
    )


def service(factory, bundle: object) -> ReadyModelRegistrationService:
    return ReadyModelRegistrationService(
        factory,
        loader=FakeLoader(bundle),
        clock=lambda: NOW,
    )


def test_ready_registration_is_exact_and_replay_is_a_noop() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    bundle = FakeBundle()
    with factory.begin() as session:
        session.add(active_model())

    with pytest.raises(ModelRegistrationRejected, match="expected training result"):
        service(factory, bundle).register(
            artifact_uri="model-ready-v1/bundle.json",
            manifest_checksum="c" * 64,
            expected_artifact_checksum="0" * 64,
        )
    with factory() as session:
        assert session.get(ModelVersion, "model-ready-v1") is None

    first = service(factory, bundle).register(
        artifact_uri="model-ready-v1/bundle.json",
        manifest_checksum="c" * 64,
    )
    replay = service(factory, bundle).register(
        artifact_uri="model-ready-v1/bundle.json",
        manifest_checksum="c" * 64,
    )
    assert first.created is True
    assert replay.created is False
    assert replay.trained_at == NOW
    with factory() as session:
        registered = session.get(ModelVersion, "model-ready-v1")
        assert registered is not None
        assert registered.data_version == "data-v1"
        assert registered.data_manifest_checksum == "a" * 64
        assert registered.config_checksum == "b" * 64
        assert registered.manifest_checksum == "c" * 64
        assert registered.artifact_checksum == "d" * 64
        assert registered.metrics == {"two_stage.recall@20": 0.25}
        assert registered.status == ModelStatus.READY
        assert (
            session.scalar(
                select(ModelVersion.model_version).where(ModelVersion.status == ModelStatus.ACTIVE)
            )
            == "active-v0"
        )
    assert bundle.smoke_calls == 3
    engine.dispose()


def test_conflict_and_rejected_bundle_leave_active_unchanged() -> None:
    engine = sqlite_engine()
    factory = factory_for(engine)
    bundle = FakeBundle()
    with factory.begin() as session:
        session.add_all(
            [
                active_model(),
                ModelVersion(
                    model_version=bundle.model_version,
                    data_version="different-data",
                    data_manifest_checksum="1" * 64,
                    config_checksum=bundle.config_checksum,
                    metrics={"two_stage.recall@20": 0.25},
                    artifact_uri="model-ready-v1/bundle.json",
                    artifact_checksum="d" * 64,
                    manifest_checksum="c" * 64,
                    purpose=EvaluationPurpose.BASE_OFFICIAL,
                    evaluation_comparability=Comparability.COMPARABLE,
                    activation_eligible=True,
                    status=ModelStatus.READY,
                    trained_at=NOW,
                ),
            ]
        )
    with pytest.raises(ModelRegistrationConflict):
        service(factory, bundle).register(
            artifact_uri="model-ready-v1/bundle.json",
            manifest_checksum="c" * 64,
        )

    systems_manifest = dict(bundle.manifest or {})
    systems_manifest.update(
        {
            "purpose": "systems_only",
            "evaluation_comparability": "non_comparable",
            "activation_eligible": False,
            "status": "EVALUATED",
        }
    )
    with pytest.raises(ModelRegistrationRejected, match="comparable activation-eligible"):
        service(factory, FakeBundle(manifest=systems_manifest)).register(
            artifact_uri="model-ready-v1/bundle.json",
            manifest_checksum="c" * 64,
        )
    with pytest.raises(ModelRegistrationRejected, match="did not return a ModelBundle"):
        service(factory, object()).register(
            artifact_uri="model-ready-v1/bundle.json",
            manifest_checksum="c" * 64,
        )
    with factory() as session:
        active = session.scalar(
            select(ModelVersion.model_version).where(ModelVersion.status == ModelStatus.ACTIVE)
        )
        assert active == "active-v0"
    engine.dispose()


def test_worker_wrapper_registers_ready_and_skips_ineligible_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_result = {
        "model_version": "model-ready-v1",
        "manifest_checksum": "c" * 64,
        "config_checksum": "b" * 64,
        "artifact_checksum": "d" * 64,
        "artifact_uri": "model-ready-v1/bundle.json",
        "model_status": "READY",
        "activation_eligible": True,
        "published": False,
        "activated": False,
    }
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("MODEL_ARTIFACT_ROOT", "/artifacts/models")
    monkeypatch.setenv("MODEL_ARTIFACTS_DIR", "/artifacts/models")
    monkeypatch.setattr(model_training, "worker_training_handler", Mock(return_value=ready_result))
    monkeypatch.setattr(model_training, "create_database_engine", Mock(return_value=Mock()))
    monkeypatch.setattr(model_training, "create_session_factory", Mock(return_value=Mock()))
    register = Mock(
        return_value=SimpleNamespace(
            **{
                key: ready_result[key]
                for key in (
                    "model_version",
                    "manifest_checksum",
                    "config_checksum",
                    "artifact_checksum",
                    "artifact_uri",
                )
            },
            created=True,
            trained_at=NOW,
        )
    )
    registration_service = Mock()
    registration_service.register = register
    monkeypatch.setattr(
        model_training,
        "ReadyModelRegistrationService",
        Mock(return_value=registration_service),
    )
    request = Mock()
    control = Mock()
    result = model_training.registered_worker_training_handler(request, control)
    assert result["ready_registered"] is True
    assert result["registration_created"] is True
    register.assert_called_once_with(
        artifact_uri="model-ready-v1/bundle.json",
        manifest_checksum="c" * 64,
        expected_model_version="model-ready-v1",
        expected_config_checksum="b" * 64,
        expected_artifact_checksum="d" * 64,
    )

    model_training.worker_training_handler.return_value = {
        **ready_result,
        "model_status": "EVALUATED",
        "activation_eligible": False,
    }
    registration_service.reset_mock()
    result = model_training.registered_worker_training_handler(request, control)
    assert result["ready_registered"] is False
    registration_service.register.assert_not_called()
