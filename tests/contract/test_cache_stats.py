from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from apps.api.app.cli import cache_stats
from apps.api.app.cli.cache_stats import collect_cache_stats


class FakeRedis:
    def __init__(self) -> None:
        self.ttls = {
            b"microlens:cache:v1:a": 10,
            b"microlens:cache:v1:b": 120,
            b"microlens:cache:v2:c": -1,
            b"microlens:cache:v2:d": -2,
        }

    def ping(self) -> bool:
        return True

    def scan_iter(self, *, match: str, count: int):
        assert match == "microlens:cache:*"
        assert count == 123
        return iter(self.ttls)

    def ttl(self, key: bytes) -> int:
        return self.ttls[key]


class CacheStatsContractTest(unittest.TestCase):
    def test_cache_stats_are_read_only_and_do_not_expose_values_or_credentials(self) -> None:
        result = collect_cache_stats(FakeRedis(), scan_count=123)
        self.assertIs(result["redis_available"], True)
        self.assertEqual(result["cache_key_count"], 4)
        self.assertEqual(result["namespace_key_counts"], {"v1": 2, "v2": 2})
        self.assertEqual(
            result["ttl_distribution"],
            {
                "0-60s": 1,
                "61-300s": 1,
                "expired_during_scan": 1,
                "persistent": 1,
            },
        )
        rendered = str(result)
        self.assertNotIn("redis://", rendered)
        self.assertIs(result["process_metrics"]["available"], False)

    def test_cli_connection_failure_suppresses_redis_url(self) -> None:
        class BrokenRedis:
            @classmethod
            def from_url(cls, url: str, **kwargs):
                raise ConnectionError(f"cannot connect to {url}")

        output = StringIO()
        secret_url = "redis://secret-user:secret-password@redis:6379/0"
        with (
            patch.dict(os.environ, {"REDIS_URL": secret_url}),
            patch.dict(sys.modules, {"redis": SimpleNamespace(Redis=BrokenRedis)}),
            redirect_stdout(output),
        ):
            self.assertEqual(cache_stats.main([]), 1)
        payload = output.getvalue()
        result = json.loads(payload)
        self.assertIs(result["redis_available"], False)
        self.assertNotIn("secret", payload)
        self.assertNotIn("redis://", payload)
