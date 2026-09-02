from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

T = TypeVar("T")


class CacheBackend(Protocol):
    """Small synchronous protocol implemented by redis-py in production."""

    def get(self, key: str) -> bytes | None: ...

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None: ...

    def delete(self, key: str) -> None: ...

    def ping(self) -> bool: ...


@dataclass(frozen=True)
class CachePolicy:
    namespace_version: str = "v1"
    ttl_seconds: int = 60
    process_fallback_ttl_seconds: int = 5

    def __post_init__(self) -> None:
        if not self.namespace_version or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in self.namespace_version
        ):
            raise ValueError("namespace_version must be a non-empty safe token")
        if self.ttl_seconds <= 0 or self.process_fallback_ttl_seconds <= 0:
            raise ValueError("cache TTLs must be positive")


@dataclass(frozen=True)
class CacheAuthority:
    """Versions read from PostgreSQL in the caller's current transaction.

    A cache lookup never accepts caller-supplied generations without first invoking
    the authority loader. Offline and permission checks therefore cannot be bypassed
    by an older Redis value.
    """

    profile_version: int
    active_model_version: str
    operations_generation: int
    permission_generation: int = 0
    allowed: bool = True
    online: bool = True

    def validate(self) -> None:
        if (
            min(
                self.profile_version,
                self.operations_generation,
                self.permission_generation,
            )
            < 0
        ):
            raise ValueError("cache generations must be non-negative")
        if not self.active_model_version or self.active_model_version == "latest":
            raise ValueError("active_model_version must be an explicit immutable version")
        if not self.allowed:
            raise AuthorityDenied("current database permissions deny this cache read")
        if not self.online:
            raise AuthorityDenied("current database item state is offline")


class AuthorityDenied(PermissionError):
    pass


@dataclass
class CacheMetrics:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    invalidations: int = 0
    backend_failures: int = 0
    process_fallback_hits: int = 0
    loads: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def increment(self, field_name: str) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + 1)

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            lookups = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "writes": self.writes,
                "invalidations": self.invalidations,
                "backend_failures": self.backend_failures,
                "process_fallback_hits": self.process_fallback_hits,
                "loads": self.loads,
                "hit_rate": self.hits / lookups if lookups else 0.0,
            }


class InMemoryCacheBackend:
    """Deterministic fake matching Redis TTL behavior for unit tests."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._clock = monotonic
        self._values: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            stored = self._values.get(key)
            if stored is None:
                return None
            expires_at, value = stored
            if expires_at <= self._clock():
                self._values.pop(key, None)
                return None
            return value

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        with self._lock:
            self._values[key] = (self._clock() + ttl_seconds, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)

    def ping(self) -> bool:
        return True


class VersionedCache:
    """Cache-aside helper with a bounded process fallback.

    PostgreSQL remains authoritative: `authority` must read the current profile,
    active-model, operations and permission generations plus online/permission state.
    Those values are included in the key and checked before Redis or process memory is
    consulted. Backend errors are observable and fall back to short-lived process
    memory or the authoritative loader.
    """

    def __init__(
        self,
        backend: CacheBackend,
        *,
        policy: CachePolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        metrics: CacheMetrics | None = None,
    ) -> None:
        self.backend = backend
        self.policy = policy or CachePolicy()
        self.metrics = metrics or CacheMetrics()
        self._clock = monotonic
        self._process_values: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def get_or_load(
        self,
        *,
        resource: str,
        authority: Callable[[], CacheAuthority],
        loader: Callable[[], T],
    ) -> T:
        current = authority()
        current.validate()
        key = self.key_for(resource=resource, authority=current)

        encoded: bytes | None = None
        try:
            encoded = self.backend.get(key)
        except Exception:
            self.metrics.increment("backend_failures")
            encoded = self._get_process(key)
            if encoded is not None:
                self.metrics.increment("process_fallback_hits")

        if encoded is not None:
            try:
                value = json.loads(encoded)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                self.metrics.increment("misses")
                self.invalidate(resource=resource, authority=current)
            else:
                confirmed = authority()
                confirmed.validate()
                if confirmed != current:
                    raise ValueError("cache authority changed during lookup; retry")
                self.metrics.increment("hits")
                return value
        else:
            self.metrics.increment("misses")

        value = loader()
        self.metrics.increment("loads")
        confirmed = authority()
        confirmed.validate()
        if confirmed != current:
            raise ValueError("cache authority changed during load; retry")
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._put_process(key, payload)
        try:
            self.backend.set(key, payload, ttl_seconds=self.policy.ttl_seconds)
            self.metrics.increment("writes")
        except Exception:
            self.metrics.increment("backend_failures")
        return value

    def invalidate(self, *, resource: str, authority: CacheAuthority) -> None:
        key = self.key_for(resource=resource, authority=authority)
        with self._lock:
            self._process_values.pop(key, None)
        try:
            self.backend.delete(key)
        except Exception:
            self.metrics.increment("backend_failures")
        self.metrics.increment("invalidations")

    def key_for(self, *, resource: str, authority: CacheAuthority) -> str:
        authority.validate()
        identity = json.dumps(
            {
                "resource": resource,
                "profile": authority.profile_version,
                "model": authority.active_model_version,
                "operations": authority.operations_generation,
                "permissions": authority.permission_generation,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"microlens:cache:{self.policy.namespace_version}:{digest}"

    def _get_process(self, key: str) -> bytes | None:
        with self._lock:
            stored = self._process_values.get(key)
            if stored is None:
                return None
            expires_at, value = stored
            if expires_at <= self._clock():
                self._process_values.pop(key, None)
                return None
            return value

    def _put_process(self, key: str, value: bytes) -> None:
        with self._lock:
            self._process_values[key] = (
                self._clock() + self.policy.process_fallback_ttl_seconds,
                value,
            )
