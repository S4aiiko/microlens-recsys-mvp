from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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


def test_platform_routes_are_public_listener_only_and_require_authentication() -> None:
    app = create_public_app()
    route_paths: list[str] = []
    for route in app.routes:
        if hasattr(route, "path"):
            route_paths.append(route.path)
        elif hasattr(route, "original_router"):
            route_paths.extend(
                nested.path for nested in route.original_router.routes if hasattr(nested, "path")
            )
    assert route_paths.index("/api/items/search") < route_paths.index("/api/items/{item_id}")
    with TestClient(app) as client:
        for method, path in (
            ("get", "/api/items/search?q=python"),
            ("get", "/api/admin/search/health"),
            ("post", "/api/admin/async-jobs"),
            ("post", "/api/admin/operation-jobs"),
            ("get", "/api/admin/alerts"),
        ):
            response = client.request(method, path)
            assert response.status_code == 401, (method, path, response.text)
            assert response.json()["code"] == "authentication_required"


def test_secure_staging_and_atomic_swap(tmp_path: Path) -> None:
    payload = b'{"captured":"exact-bytes"}'
    artifact = tmp_path / "model.json"
    artifact.write_bytes(payload)
    loader = SecureJsonStagingLoader(tmp_path)
    staged_bundle = object()
    with patch.object(
        SecureJsonStagingLoader,
        "_load_captured",
        return_value=staged_bundle,
    ) as load_captured:
        staged = loader.stage(
            artifact_uri="model.json",
            artifact_checksum=hashlib.sha256(payload).hexdigest(),
            manifest_checksum="b" * 64,
        )
    load_captured.assert_called_once_with(payload, "b" * 64)
    slot = AtomicRuntimeModelSlot()
    slot.swap(model_version="model-v1", staged_bundle=staged)
    assert slot.snapshot() == ("model-v1", staged_bundle)

    artifact.write_bytes(b'{"tampered":true}')
    with pytest.raises(ValueError, match="artifact checksum mismatch"):
        loader.stage(
            artifact_uri="model.json",
            artifact_checksum=hashlib.sha256(payload).hexdigest(),
            manifest_checksum="b" * 64,
        )

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
    payload = b'{"captured":"active"}'
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
                data_manifest_checksum="e" * 64,
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
    staged_resource = object()
    runtime = RuntimeContext(settings=settings, engine=engine, sessions=sessions, redis=None)
    with (
        patch(
            "apps.api.app.feeds.resources.RecommendationResourceStagingLoader.stage_activation",
            return_value=staged_resource,
        ),
        patch("apps.api.app.feeds.resources.sync_serving_resource"),
    ):
        runtime.restore_active_model()
    assert runtime.active_restore_status == "restored"
    assert runtime.model_slot.snapshot() == ("restored-v1", staged_resource)
    engine.dispose()
