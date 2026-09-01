from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from apps.api.app.auth.rate_limit import (
    RedisRegistrationLimiter,
    RegistrationLimiterUnavailable,
)

REDIS_URL = os.environ.get("PHASE2D_REDIS_URL")


class RealRedisRegistrationLimiterTests(unittest.TestCase):
    @unittest.skipUnless(REDIS_URL, "set PHASE2D_REDIS_URL to an isolated Redis database")
    def test_atomic_lifecycle_expiry_and_dirty_key_recovery(self) -> None:
        from redis.asyncio import Redis

        async def exercise() -> None:
            client = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            namespace = f"microlens:test:register:{uuid.uuid4().hex}"
            limiter = RedisRegistrationLimiter(
                client,
                limit=2,
                window_seconds=1,
                namespace=namespace,
            )
            key = limiter._key("first")
            concurrent_key = limiter._key("concurrent")
            dirty_key = limiter._key("dirty")
            negative_ttl_key = limiter._key("negative-with-ttl")
            negative_no_ttl_key = limiter._key("negative-without-ttl")
            max_key = limiter._key("maximum")
            overflow_key = limiter._key("overflow")
            malformed_key = limiter._key("malformed")
            try:
                self.assertTrue(await limiter.allow("first"))
                self.assertEqual(await client.get(key), "1")

                concurrent_results = await asyncio.gather(
                    *(limiter.allow("concurrent") for _ in range(20))
                )
                self.assertEqual(concurrent_results.count(True), 2)
                self.assertEqual(concurrent_results.count(False), 18)
                self.assertEqual(await client.get(concurrent_key), "20")
                concurrent_ttl = await client.pttl(concurrent_key)
                self.assertLessEqual(concurrent_ttl, 1_000)
                self.assertGreater(concurrent_ttl, 0)
                first_ttl = await client.pttl(key)
                self.assertLessEqual(first_ttl, 1_000)
                self.assertGreater(first_ttl, 0)

                self.assertTrue(await limiter.allow("first"))
                self.assertFalse(await limiter.allow("first"))
                self.assertEqual(await client.get(key), "3")
                current_ttl = await client.pttl(key)
                self.assertLessEqual(current_ttl, first_ttl)
                self.assertGreater(current_ttl, 0)

                await asyncio.sleep(1.1)
                self.assertTrue(await limiter.allow("first"))
                self.assertEqual(await client.get(key), "1")

                await client.set(dirty_key, 99)
                self.assertEqual(await client.ttl(dirty_key), -1)
                self.assertFalse(await limiter.allow("dirty"))
                self.assertEqual(await client.get(dirty_key), "100")
                dirty_ttl = await client.pttl(dirty_key)
                self.assertLessEqual(dirty_ttl, 1_000)
                self.assertGreater(dirty_ttl, 0)
                await asyncio.sleep(1.1)
                self.assertTrue(await limiter.allow("dirty"))
                self.assertEqual(await client.get(dirty_key), "1")

                await client.set(negative_ttl_key, -2, ex=30)
                for _ in range(3):
                    with self.assertRaisesRegex(
                        RegistrationLimiterUnavailable,
                        "registration limiter unavailable",
                    ) as negative_ttl_error:
                        await limiter.allow("negative-with-ttl")
                self.assertNotIn(REDIS_URL, str(negative_ttl_error.exception))
                self.assertEqual(await client.get(negative_ttl_key), "-2")
                self.assertGreater(await client.ttl(negative_ttl_key), 0)

                await client.set(negative_no_ttl_key, -2)
                for _ in range(3):
                    with self.assertRaisesRegex(
                        RegistrationLimiterUnavailable,
                        "registration limiter unavailable",
                    ) as negative_no_ttl_error:
                        await limiter.allow("negative-without-ttl")
                self.assertNotIn(REDIS_URL, str(negative_no_ttl_error.exception))
                self.assertEqual(await client.get(negative_no_ttl_key), "-2")
                negative_no_ttl_ttl = await client.pttl(negative_no_ttl_key)
                self.assertLessEqual(negative_no_ttl_ttl, 1_000)
                self.assertGreater(negative_no_ttl_ttl, 0)

                await client.set(max_key, (1 << 63) - 2, ex=30)
                self.assertFalse(await limiter.allow("maximum"))
                self.assertEqual(await client.get(max_key), str((1 << 63) - 1))

                await client.set(overflow_key, (1 << 63) - 1, ex=30)
                with self.assertRaisesRegex(
                    RegistrationLimiterUnavailable,
                    "registration limiter unavailable",
                ) as overflow_error:
                    await limiter.allow("overflow")
                self.assertNotIn(REDIS_URL, str(overflow_error.exception))
                self.assertEqual(await client.get(overflow_key), str((1 << 63) - 1))

                await client.set(malformed_key, "not-an-integer")
                with self.assertRaises(RegistrationLimiterUnavailable):
                    await limiter.allow("malformed")
                malformed_ttl = await client.pttl(malformed_key)
                self.assertLessEqual(malformed_ttl, 1_000)
                self.assertGreater(malformed_ttl, 0)
            finally:
                await client.delete(
                    key,
                    concurrent_key,
                    dirty_key,
                    negative_ttl_key,
                    negative_no_ttl_key,
                    max_key,
                    overflow_key,
                    malformed_key,
                )
                await client.aclose()

        asyncio.run(exercise())

    def test_connection_failure_is_closed(self) -> None:
        from redis.asyncio import Redis

        async def exercise() -> None:
            client = Redis.from_url(
                "redis://127.0.0.1:1/0",
                socket_connect_timeout=0.1,
                socket_timeout=0.1,
            )
            try:
                with self.assertRaises(RegistrationLimiterUnavailable):
                    await RedisRegistrationLimiter(client).allow("connection-failure")
            finally:
                await client.aclose()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
