from __future__ import annotations

import argparse
import json
import os
import uuid

from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    TrainingJob,
    TrainingJobStatus,
)
from apps.api.app.db.session import create_database_engine, create_session_factory
from apps.api.app.models_registry.repository import ModelRegistryRepository


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Enqueue an explicit immutable training job")
    command.add_argument("--idempotency-key", required=True)
    command.add_argument("--data-version", required=True)
    command.add_argument("--data-manifest-checksum", required=True)
    command.add_argument("--config-checksum", required=True)
    command.add_argument(
        "--purpose",
        required=True,
        choices=[purpose.value for purpose in EvaluationPurpose],
    )
    command.add_argument(
        "--evaluation-comparability",
        required=True,
        choices=[value.value for value in Comparability],
    )
    command.add_argument("--activation-eligible", choices=("true", "false"), required=True)
    return command


def _checksum(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.data_version.lower() in {"latest", ".", ".."}:
        raise ValueError("data-version must be explicit and immutable, never latest")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        sessions = create_session_factory(engine)
        requested = TrainingJob(
            job_id=uuid.uuid4(),
            idempotency_key=arguments.idempotency_key,
            data_version=arguments.data_version,
            data_manifest_checksum=_checksum(
                arguments.data_manifest_checksum, "data-manifest-checksum"
            ),
            config_checksum=_checksum(arguments.config_checksum, "config-checksum"),
            purpose=EvaluationPurpose(arguments.purpose),
            evaluation_comparability=Comparability(arguments.evaluation_comparability),
            activation_eligible=arguments.activation_eligible == "true",
            status=TrainingJobStatus.QUEUED,
        )
        with sessions.begin() as session:
            job = ModelRegistryRepository().enqueue_job(session, requested)
            payload = {
                "job_id": str(job.job_id),
                "idempotency_key": job.idempotency_key,
                "data_version": job.data_version,
                "config_checksum": job.config_checksum,
                "status": job.status.value,
            }
        print(json.dumps(payload, sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
