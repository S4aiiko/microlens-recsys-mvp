from __future__ import annotations

import uuid
from typing import Any


class RedisPyBroker:
    """redis-py notification adapter.

    Messages are hints only. A lost or duplicate message is safe because claims and
    all state transitions are performed against PostgreSQL.
    """

    def __init__(self, client: Any, *, queue_key: str = "microlens:jobs:v1:notify") -> None:
        self._client = client
        self.queue_key = queue_key

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        queue_key: str = "microlens:jobs:v1:notify",
        socket_timeout_seconds: float = 1.0,
    ) -> RedisPyBroker:
        if not url:
            raise ValueError("Redis URL must be explicit")
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - platform dependency gate
            raise RuntimeError("redis-py is required for the production broker") from exc
        client = redis.Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            health_check_interval=30,
        )
        return cls(client, queue_key=queue_key)

    def notify(self, job_id: uuid.UUID) -> None:
        self._client.rpush(self.queue_key, str(job_id).encode("ascii"))

    def receive(self, *, timeout_seconds: int = 0) -> uuid.UUID | None:
        if timeout_seconds > 0:
            item = self._client.blpop(self.queue_key, timeout=timeout_seconds)
            raw = None if item is None else item[1]
        else:
            raw = self._client.lpop(self.queue_key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("ascii")
        try:
            return uuid.UUID(str(raw))
        except ValueError:
            return None

    def ping(self) -> bool:
        return bool(self._client.ping())
