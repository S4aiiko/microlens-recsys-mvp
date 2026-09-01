from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.db.models import ModelStatus, ModelVersion
from apps.api.app.db.session import create_database_engine, create_session_factory
from apps.api.app.settings import AppSettings

MAX_BUNDLE_BYTES = 16 * 1024 * 1024


class AtomicRuntimeModelSlot:
    """A process-local atomic bundle reference shared by both API listeners."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model_version: str | None = None
        self._bundle: object | None = None

    def swap(self, *, model_version: str, staged_bundle: object) -> None:
        # All validation and I/O happen before this bounded assignment section.
        with self._lock:
            self._model_version, self._bundle = model_version, staged_bundle

    def snapshot(self) -> tuple[str | None, object | None]:
        with self._lock:
            return self._model_version, self._bundle


@dataclass(frozen=True)
class SecureJsonStagingLoader:
    """Stage a bounded immutable JSON bundle without following symlinks."""

    artifact_root: Path

    def stage(self, *, artifact_uri: str, artifact_checksum: str, manifest_checksum: str) -> object:
        relative = PurePosixPath(artifact_uri)
        if relative.is_absolute() or not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("artifact_uri must be a safe relative path")
        root = self.artifact_root.resolve(strict=True)
        candidate = root.joinpath(*relative.parts)
        if candidate.is_symlink():
            raise ValueError("artifact bundle cannot be a symlink")
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents:
            raise ValueError("artifact path escapes the configured artifact root")
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("artifact bundle must be a regular file")
            if metadata.st_size <= 0 or metadata.st_size > MAX_BUNDLE_BYTES:
                raise ValueError("artifact bundle size is outside the safe staging bound")
            payload = bytearray()
            while len(payload) <= MAX_BUNDLE_BYTES:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_BUNDLE_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
        finally:
            os.close(descriptor)
        if len(payload) > MAX_BUNDLE_BYTES:
            raise ValueError("artifact bundle exceeds the safe staging bound")
        if hashlib.sha256(payload).hexdigest() != artifact_checksum:
            raise ValueError("artifact checksum mismatch")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("artifact bundle must be a JSON object")
        if document.get("manifest_checksum") != manifest_checksum:
            raise ValueError("staged bundle manifest checksum mismatch")
        return document


@dataclass
class RuntimeContext:
    settings: AppSettings
    engine: Engine
    sessions: sessionmaker[Session]
    redis: Any | None
    model_slot: AtomicRuntimeModelSlot = field(default_factory=AtomicRuntimeModelSlot)
    active_restore_status: str = "not_checked"
    active_restore_error: str | None = None

    def readiness(self) -> tuple[bool, dict[str, object]]:
        if not self.settings.configured:
            return False, {
                "configuration": "missing_runtime_environment",
                "database": "not_checked",
                "alembic": "not_checked",
                "redis": "not_checked",
                "active_model_restore": self.active_restore_status,
            }
        checks: dict[str, object] = {}
        ready = True
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                checks["database"] = "ok"
                current = MigrationContext.configure(connection).get_current_revision()
            config = Config(str(self.settings.alembic_ini))
            head = ScriptDirectory.from_config(config).get_current_head()
            checks["alembic"] = {"current": current, "head": head, "at_head": current == head}
            ready = ready and current == head and head is not None
        except Exception as exc:
            checks["database"] = "unavailable"
            checks["alembic"] = {"at_head": False, "error": type(exc).__name__}
            ready = False
        try:
            if self.redis is None:
                raise RuntimeError("redis client is not configured")
            # The public readiness endpoint is sync; redis-py exposes a sync-free
            # connection pool, so the live async PING is performed by lifespan/server
            # probes and Redis-backed HTTP tests. Here we report configured ownership.
            checks["redis"] = "configured"
        except Exception as exc:
            checks["redis"] = f"unavailable:{type(exc).__name__}"
            ready = False
        checks["active_model_restore"] = self.active_restore_status
        if self.active_restore_error:
            checks["active_model_restore_error"] = self.active_restore_error
            ready = False
        return ready, checks

    def restore_active_model(self) -> None:
        """Reload DB ACTIVE at process start; fail readiness if the bundle is invalid."""

        if not self.settings.configured:
            self.active_restore_status = "not_configured"
            return
        loader = SecureJsonStagingLoader(self.settings.model_artifacts_dir)
        try:
            with self.sessions() as session:
                active = session.scalar(
                    select(ModelVersion).where(ModelVersion.status == ModelStatus.ACTIVE)
                )
                if active is None:
                    self.active_restore_status = "no_active_model"
                    return
                bundle = loader.stage(
                    artifact_uri=active.artifact_uri,
                    artifact_checksum=active.artifact_checksum,
                    manifest_checksum=active.manifest_checksum,
                )
                self.model_slot.swap(model_version=active.model_version, staged_bundle=bundle)
                self.active_restore_status = "restored"
        except Exception as exc:
            self.active_restore_status = "failed"
            self.active_restore_error = type(exc).__name__

    async def ping_redis(self) -> bool:
        if self.redis is None:
            return False
        return bool(await self.redis.ping())

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
        self.engine.dispose()


def create_runtime(settings: AppSettings) -> RuntimeContext:
    engine = create_database_engine(settings.database_url)
    redis_client = None
    if settings.configured:
        from redis.asyncio import Redis

        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=15,
        )
    return RuntimeContext(
        settings=settings,
        engine=engine,
        sessions=create_session_factory(engine),
        redis=redis_client,
    )
