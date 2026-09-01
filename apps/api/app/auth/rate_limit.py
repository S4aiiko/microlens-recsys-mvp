from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


class RegistrationLimiter(Protocol):
    async def allow(self, identity: str) -> bool: ...


class AsyncRedisCounter(Protocol):
    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> object: ...


class RegistrationLimiterUnavailable(RuntimeError):
    pass


_ATOMIC_INCREMENT_WITH_EXPIRY = """
local ttl = redis.call("TTL", KEYS[1])
if ttl == -2 then
    redis.call("SET", KEYS[1], 1, "EX", ARGV[1])
    return 1
end
if ttl == -1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end
local current = redis.call("GET", KEYS[1])
local numeric = tonumber(current)
if numeric ~= nil and numeric <= 0 then
    return numeric
end
return redis.call("INCR", KEYS[1])
""".strip()

_REDIS_SIGNED_INTEGER_MAX = (1 << 63) - 1


@dataclass(frozen=True)
class RedisRegistrationLimiter:
    """Fixed-window limiter executed as one atomic Redis operation.

    A legacy integer counter without a TTL is given a bounded recovery window before
    it is incremented. Malformed keys and Redis failures fail registration closed.
    """

    redis: AsyncRedisCounter
    limit: int = 5
    window_seconds: int = 300
    namespace: str = "microlens:auth:register"

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("registration limit must be positive")
        if self.window_seconds < 1:
            raise ValueError("registration window must be positive")
        if not self.namespace:
            raise ValueError("registration limiter namespace cannot be empty")

    def _key(self, identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{self.namespace}:{digest}"

    async def allow(self, identity: str) -> bool:
        key = self._key(identity)
        try:
            result = await self.redis.eval(
                _ATOMIC_INCREMENT_WITH_EXPIRY,
                1,
                key,
                self.window_seconds,
            )
            if (
                not isinstance(result, int)
                or isinstance(result, bool)
                or result < 1
                or result > _REDIS_SIGNED_INTEGER_MAX
            ):
                raise ValueError("invalid Redis counter response")
            count = result
        except Exception as exc:
            raise RegistrationLimiterUnavailable("registration limiter unavailable") from exc
        return count <= self.limit


@dataclass
class InMemoryRegistrationLimiter:
    """Deterministic test adapter; production must inject RedisRegistrationLimiter."""

    limit: int = 5

    def __post_init__(self) -> None:
        self._counts: dict[str, int] = {}

    async def allow(self, identity: str) -> bool:
        self._counts[identity] = self._counts.get(identity, 0) + 1
        return self._counts[identity] <= self.limit
