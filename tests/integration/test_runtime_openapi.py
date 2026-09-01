from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app.db import Base
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    ModelStatus,
    ModelVersion,
)
from apps.api.app.internal_main import create_internal_app
from apps.api.app.main import create_public_app
from apps.api.app.runtime import AtomicRuntimeModelSlot, RuntimeContext, SecureJsonStagingLoader
from apps.api.app.settings import AppSettings

ROOT = Path(__file__).resolve().parents[2]


def test_generated_contracts_are_the_runtime_schemas() -> None:
    public = create_public_app().openapi()
    internal = create_internal_app().openapi()
    assert public == json.loads((ROOT / "docs/contracts/openapi.json").read_text())
    assert internal == json.loads((ROOT / "docs/contracts/internal-openapi.json").read_text())


def test_public_and_internal_route_sets_are_disjoint() -> None:
    public = create_public_app().openapi()
    internal = create_internal_app().openapi()
    assert all(not path.startswith("/internal/") for path in public["paths"])
    assert set(internal["paths"]) == {"/internal/model-versions/{version}/activate"}
    encoded_public = json.dumps(public).lower()
    assert "publish_token" not in encoded_public
    assert "x-publish-token" not in encoded_public

    client = TestClient(create_public_app())
    assert client.get("/internal/model-versions/example/activate").status_code == 404
    deferred = client.get("/api/feeds/popular")
    assert deferred.status_code == 401
    assert deferred.json()["code"] == "authentication_required"


def test_secure_staging_and_atomic_swap(tmp_path: Path) -> None:
    document = {"manifest_checksum": "b" * 64, "weights": [1, 2, 3]}
    payload = json.dumps(document, separators=(",", ":")).encode()
    artifact = tmp_path / "model.json"
    artifact.write_bytes(payload)
    loader = SecureJsonStagingLoader(tmp_path)
    staged = loader.stage(
        artifact_uri="model.json",
        artifact_checksum=hashlib.sha256(payload).hexdigest(),
        manifest_checksum="b" * 64,
    )
    slot = AtomicRuntimeModelSlot()
    slot.swap(model_version="model-v1", staged_bundle=staged)
    assert slot.snapshot() == ("model-v1", document)

    outside = tmp_path.parent / "outside-model.json"
    outside.write_bytes(payload)
    try:
        with pytest.raises(ValueError, match="safe relative path"):
            loader.stage(
                artifact_uri="../outside-model.json",
                artifact_checksum=hashlib.sha256(payload).hexdigest(),
                manifest_checksum="b" * 64,
            )
    finally:
        outside.unlink()


def test_restart_restores_the_database_active_model_into_the_process_slot(
    tmp_path: Path,
) -> None:
    manifest_checksum = "d" * 64
    document = {"manifest_checksum": manifest_checksum, "weights": [0.5]}
    payload = json.dumps(document, separators=(",", ":")).encode()
    (tmp_path / "active.json").write_bytes(payload)
    settings = replace(
        AppSettings.from_environment(allow_unconfigured=True),
        configured=True,
        model_artifacts_dir=tmp_path,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            ModelVersion(
                model_version="restored-v1",
                data_version="test-data",
                config_checksum="a" * 64,
                metrics={},
                artifact_uri="active.json",
                artifact_checksum=hashlib.sha256(payload).hexdigest(),
                manifest_checksum=manifest_checksum,
                purpose=EvaluationPurpose.BASE_OFFICIAL,
                evaluation_comparability=Comparability.COMPARABLE,
                activation_eligible=True,
                status=ModelStatus.ACTIVE,
                trained_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
        )
    runtime = RuntimeContext(settings=settings, engine=engine, sessions=sessions, redis=None)
    runtime.restore_active_model()
    assert runtime.active_restore_status == "restored"
    assert runtime.model_slot.snapshot() == ("restored-v1", document)
    engine.dispose()
