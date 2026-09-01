from __future__ import annotations

import asyncio
import unittest

from apps.api.app.auth.rate_limit import (
    RedisRegistrationLimiter,
    RegistrationLimiterUnavailable,
)


class FakeAtomicRedis:
    def __init__(self, *, value: int | None = None, has_ttl: bool = False) -> None:
        self.value = value
        self.has_ttl = has_ttl
        self.calls: list[tuple[str, int, tuple[str | int, ...]]] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> object:
        self.calls.append((script, numkeys, keys_and_args))
        if self.value is None:
            self.value = 1
            self.has_ttl = True
            return self.value
        if not self.has_ttl:
            self.has_ttl = True
        if self.value <= 0:
            return self.value
        self.value += 1
        return self.value


class FailingAtomicRedis:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def eval(
        self,
        _script: str,
        _numkeys: int,
        *_keys_and_args: str | int,
    ) -> object:
        raise self.error


class ReturningAtomicRedis:
    def __init__(self, result: object) -> None:
        self.result = result

    async def eval(
        self,
        _script: str,
        _numkeys: int,
        *_keys_and_args: str | int,
    ) -> object:
        return self.result


class RedisRegistrationLimiterTests(unittest.TestCase):
    def test_uses_one_eval_per_attempt_and_enforces_limit(self) -> None:
        redis = FakeAtomicRedis()
        limiter = RedisRegistrationLimiter(redis, limit=2, window_seconds=30)

        self.assertTrue(asyncio.run(limiter.allow("203.0.113.4")))
        self.assertTrue(asyncio.run(limiter.allow("203.0.113.4")))
        self.assertFalse(asyncio.run(limiter.allow("203.0.113.4")))

        self.assertEqual(len(redis.calls), 3)
        for script, numkeys, keys_and_args in redis.calls:
            self.assertIn('redis.call("SET", KEYS[1], 1, "EX", ARGV[1])', script)
            self.assertEqual(numkeys, 1)
            self.assertEqual(len(keys_and_args), 2)
            self.assertEqual(keys_and_args[-1], 30)
        self.assertTrue(redis.has_ttl)

    def test_existing_no_ttl_counter_gets_bounded_recovery_window(self) -> None:
        redis = FakeAtomicRedis(value=99, has_ttl=False)
        limiter = RedisRegistrationLimiter(redis, limit=5, window_seconds=45)

        self.assertFalse(asyncio.run(limiter.allow("legacy-key")))

        self.assertEqual(redis.value, 100)
        self.assertTrue(redis.has_ttl)
        self.assertEqual(len(redis.calls), 1)

    def test_dirty_negative_counter_remains_closed_until_expiry(self) -> None:
        redis = FakeAtomicRedis(value=-2, has_ttl=False)
        limiter = RedisRegistrationLimiter(redis, limit=5, window_seconds=45)

        for _ in range(3):
            with self.assertRaises(RegistrationLimiterUnavailable):
                asyncio.run(limiter.allow("dirty-key"))

        self.assertEqual(redis.value, -2)
        self.assertTrue(redis.has_ttl)
        self.assertEqual(len(redis.calls), 3)

    def test_connection_and_script_errors_fail_closed(self) -> None:
        for error in (ConnectionError("unavailable"), RuntimeError("script failed")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(RegistrationLimiterUnavailable):
                    asyncio.run(RedisRegistrationLimiter(FailingAtomicRedis(error)).allow("client"))

    def test_invalid_counter_responses_fail_closed(self) -> None:
        invalid_results = (-1, 0, "1", b"1", 1.0, True, 1 << 63)
        for result in invalid_results:
            with self.subTest(result=repr(result)):
                limiter = RedisRegistrationLimiter(ReturningAtomicRedis(result))
                with self.assertRaisesRegex(
                    RegistrationLimiterUnavailable,
                    "registration limiter unavailable",
                ):
                    asyncio.run(limiter.allow("client"))

    def test_redis_maximum_counter_is_denied_without_conversion(self) -> None:
        limiter = RedisRegistrationLimiter(ReturningAtomicRedis((1 << 63) - 1))

        self.assertFalse(asyncio.run(limiter.allow("client")))

    def test_invalid_configuration_is_rejected(self) -> None:
        redis = FakeAtomicRedis()
        with self.assertRaisesRegex(ValueError, "limit"):
            RedisRegistrationLimiter(redis, limit=0)
        with self.assertRaisesRegex(ValueError, "window"):
            RedisRegistrationLimiter(redis, window_seconds=0)
        with self.assertRaisesRegex(ValueError, "namespace"):
            RedisRegistrationLimiter(redis, namespace="")


if __name__ == "__main__":
    unittest.main()
