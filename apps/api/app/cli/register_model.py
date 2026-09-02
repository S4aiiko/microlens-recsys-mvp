from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from apps.api.app.db.session import create_database_engine, create_session_factory
from apps.api.app.models_registry.registration import ReadyModelRegistrationService
from apps.api.app.runtime import SecureJsonStagingLoader


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Validate and transactionally register READY")
    command.add_argument("--artifact-uri", required=True)
    command.add_argument("--manifest-checksum", required=True)
    command.add_argument(
        "--artifact-root",
        default=os.environ.get("MODEL_ARTIFACTS_DIR", "/artifacts/models"),
    )
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        service = ReadyModelRegistrationService(
            create_session_factory(engine),
            loader=SecureJsonStagingLoader(Path(arguments.artifact_root)),
        )
        receipt = service.register(
            artifact_uri=arguments.artifact_uri,
            manifest_checksum=arguments.manifest_checksum,
        )
        payload = asdict(receipt)
        payload["status"] = receipt.status.value
        payload["trained_at"] = receipt.trained_at.isoformat()
        print(json.dumps(payload, sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
