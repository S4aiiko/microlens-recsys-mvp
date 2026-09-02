from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from apps.api.app.db.session import create_database_engine, create_session_factory
from apps.api.app.models_registry.registration import ReadyModelRegistrationService
from apps.api.app.runtime import SecureJsonStagingLoader
from recsys.models.entrypoint import worker_training_handler

from .contracts import TrainingControl, TrainingRequest


def registered_worker_training_handler(
    request: TrainingRequest,
    control: TrainingControl,
) -> dict[str, Any]:
    """Train using the shared entrypoint, then register eligible READY output only."""

    result = worker_training_handler(request, control)
    if result.get("model_status") != "READY":
        return {**result, "ready_registered": False, "registration_created": False}
    database_url = os.environ.get("DATABASE_URL")
    artifact_root = os.environ.get("MODEL_ARTIFACT_ROOT")
    registration_root = os.environ.get("MODEL_ARTIFACTS_DIR")
    if not database_url or not artifact_root or not registration_root:
        raise RuntimeError("DATABASE_URL, MODEL_ARTIFACT_ROOT and MODEL_ARTIFACTS_DIR are required")
    if Path(artifact_root).resolve() != Path(registration_root).resolve():
        raise ValueError("training and registration artifact roots must be identical")
    engine = create_database_engine(database_url)
    try:
        receipt = ReadyModelRegistrationService(
            create_session_factory(engine),
            loader=SecureJsonStagingLoader(Path(registration_root)),
        ).register(
            artifact_uri=str(result["artifact_uri"]),
            manifest_checksum=str(result["manifest_checksum"]),
            expected_model_version=str(result["model_version"]),
            expected_config_checksum=str(result["config_checksum"]),
            expected_artifact_checksum=str(result["artifact_checksum"]),
        )
    finally:
        engine.dispose()
    exact = {
        "model_version": receipt.model_version,
        "manifest_checksum": receipt.manifest_checksum,
        "config_checksum": receipt.config_checksum,
        "artifact_checksum": receipt.artifact_checksum,
        "artifact_uri": receipt.artifact_uri,
    }
    if any(result.get(field) != value for field, value in exact.items()):
        raise ValueError("training result differs from its READY registration receipt")
    return {
        **result,
        "ready_registered": True,
        "registration_created": receipt.created,
        "trained_at": receipt.trained_at.isoformat(),
    }
