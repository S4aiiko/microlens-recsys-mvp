from __future__ import annotations

from typing import Any


class RedisPyCacheBackend:
    """Production adapter for a redis-py synchronous client.

    `redis` is intentionally imported only by `from_url`; Phase 2D platform owns
    the exact dependency declaration and hash lock.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, *, socket_timeout_seconds: float = 1.0) -> RedisPyCacheBackend:
        if not url:
            raise ValueError("Redis URL must be explicit")
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - exercised after platform locking
            raise RuntimeError("redis-py is required for the production cache adapter") from exc
        return cls(
            redis.Redis.from_url(
                url,
                decode_responses=False,
                socket_connect_timeout=socket_timeout_seconds,
                socket_timeout=socket_timeout_seconds,
                health_check_interval=30,
            )
        )

    def get(self, key: str) -> bytes | None:
        value = self._client.get(key)
        if value is None:
            return None
        return value if isinstance(value, bytes) else str(value).encode("utf-8")

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        self._client.set(key, value, ex=ttl_seconds)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def ping(self) -> bool:
        return bool(self._client.ping())
