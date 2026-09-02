from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from .repository import SqlAlchemyAsyncRepository
from .service import DurableJobService


class RedisJsonHintSink:
    """Lossy Redis list notification; PostgreSQL polling remains authoritative."""

    def __init__(self, client: Any, *, queue_key: str = "microlens:async:v1:hints") -> None:
        if not queue_key or len(queue_key) > 255:
            raise ValueError("queue_key must contain 1..255 characters")
        self.client = client
        self.queue_key = queue_key

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        *,
        queue_key: str = "microlens:async:v1:hints",
        socket_timeout_seconds: float = 1.0,
    ) -> RedisJsonHintSink:
        if not redis_url:
            raise ValueError("redis_url must be explicit")
        if socket_timeout_seconds <= 0:
            raise ValueError("socket_timeout_seconds must be positive")
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - production dependency gate
            raise RuntimeError("redis-py is required for Redis hint delivery") from exc
        client = redis.Redis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            health_check_interval=30,
        )
        return cls(client, queue_key=queue_key)

    def notify(self, topic: str, payload: dict[str, Any]) -> None:
        document = {"topic": topic, "payload": payload}
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.client.rpush(self.queue_key, encoded)

    def ping(self) -> bool:
        return bool(self.client.ping())


@dataclass(frozen=True)
class AsyncRuntime:
    repository: SqlAlchemyAsyncRepository
    jobs: DurableJobService
    hint_sink: RedisJsonHintSink | None


def create_async_runtime(
    sessions: sessionmaker[Session],
    *,
    redis_url: str | None = None,
    redis_client: Any | None = None,
    lease_seconds: int = 60,
) -> AsyncRuntime:
    if redis_url and redis_client is not None:
        raise ValueError("provide redis_url or redis_client, not both")
    repository = SqlAlchemyAsyncRepository(sessions)
    sink: RedisJsonHintSink | None = None
    if redis_client is not None:
        sink = RedisJsonHintSink(redis_client)
    elif redis_url:
        sink = RedisJsonHintSink.from_url(redis_url)
    return AsyncRuntime(
        repository=repository,
        jobs=DurableJobService(repository, hint_sink=sink, lease_seconds=lease_seconds),
        hint_sink=sink,
    )
