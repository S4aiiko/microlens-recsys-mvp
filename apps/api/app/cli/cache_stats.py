from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Any

MATCH = "microlens:cache:*"


def collect_cache_stats(client: Any, *, scan_count: int = 500) -> dict[str, Any]:
    """Read cache metadata without reading values or mutating Redis."""
    client.ping()
    namespaces: Counter[str] = Counter()
    ttl_buckets: Counter[str] = Counter()
    scanned = 0
    for raw_key in client.scan_iter(match=MATCH, count=scan_count):
        key = raw_key.decode("utf-8", "replace") if isinstance(raw_key, bytes) else str(raw_key)
        parts = key.split(":", 4)
        namespace = parts[2] if len(parts) >= 4 else "malformed"
        namespaces[namespace] += 1
        scanned += 1
        ttl = int(client.ttl(raw_key))
        if ttl == -1:
            ttl_buckets["persistent"] += 1
        elif ttl < 0:
            ttl_buckets["expired_during_scan"] += 1
        elif ttl <= 60:
            ttl_buckets["0-60s"] += 1
        elif ttl <= 300:
            ttl_buckets["61-300s"] += 1
        elif ttl <= 3600:
            ttl_buckets["301-3600s"] += 1
        else:
            ttl_buckets[">3600s"] += 1
    return {
        "redis_available": True,
        "match": MATCH,
        "cache_key_count": scanned,
        "namespace_key_counts": dict(sorted(namespaces.items())),
        "ttl_distribution": dict(sorted(ttl_buckets.items())),
        "process_metrics": {
            "available": False,
            "reason": (
                "CacheMetrics is process-local; this one-shot CLI cannot read counters "
                "from API/worker processes."
            ),
        },
        "safety": (
            "read-only: PING, SCAN and TTL only; values and connection credentials "
            "are never printed"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Redis cache namespace and TTL statistics"
    )
    parser.add_argument("--scan-count", type=int, default=500)
    args = parser.parse_args(argv)
    if args.scan_count <= 0:
        parser.error("--scan-count must be positive")
    try:
        import redis

        client = redis.Redis.from_url(
            os.environ["REDIS_URL"],
            decode_responses=False,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )
        result = collect_cache_stats(client, scan_count=args.scan_count)
    except Exception as exc:
        result = {
            "redis_available": False,
            "error_type": type(exc).__name__,
            "message": "Redis cache statistics are unavailable; connection details are suppressed.",
            "process_metrics": {
                "available": False,
                "reason": (
                    "CacheMetrics is process-local; this one-shot CLI cannot read counters "
                    "from API/worker processes."
                ),
            },
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        if "client" in locals():
            client.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
