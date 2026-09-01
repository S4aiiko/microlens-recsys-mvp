from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.async_runtime.repository import SqlAlchemyAsyncRepository
from apps.api.app.async_runtime.service import DurableJobService
from apps.api.app.async_runtime.tables import AsyncRuntimeBase

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def runtime() -> tuple[sessionmaker[Session], SqlAlchemyAsyncRepository, DurableJobService]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AsyncRuntimeBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    repository = SqlAlchemyAsyncRepository(factory)
    return factory, repository, DurableJobService(repository, lease_seconds=10)


class RecordingHintSink:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.messages: list[tuple[str, dict[str, object]]] = []

    def notify(self, topic: str, payload: dict[str, object]) -> None:
        self.messages.append((topic, payload))
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("redis unavailable")
