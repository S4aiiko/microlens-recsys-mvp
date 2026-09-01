from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

try:
    from fastapi.testclient import TestClient

    from apps.api.app.main import app
except (ModuleNotFoundError, RuntimeError):
    TestClient = None
    app = None

try:
    from apps.worker import app as worker_app
    from apps.worker.runtime import Readiness
except ModuleNotFoundError:
    worker_app = None
    Readiness = None


class ApiHealthTest(unittest.TestCase):
    def test_api_health_and_foundation_readiness(self) -> None:
        if TestClient is None or app is None:
            self.skipTest(
                "FastAPI test dependencies are not installed in the base host interpreter"
            )
        with TestClient(app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")
            ready = client.get("/ready")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["status"], "not_ready")
            self.assertEqual(ready.json()["checks"]["configuration"], "missing_runtime_environment")
            self.assertFalse(ready.json()["business_routes_ready"])


class WorkerHealthTest(unittest.TestCase):
    def test_worker_health_surface_is_explicitly_unconfigured_without_starting_it(self) -> None:
        if worker_app is None or Readiness is None:
            self.skipTest("worker runtime dependencies are not installed")

        class UnconfiguredRuntime:
            def readiness(self):
                return Readiness(
                    database=False,
                    redis=False,
                    redis_degraded=True,
                    task_processing_enabled=False,
                    scheduled_ops_runtime_configured=False,
                    training_handler_configured=False,
                    training_jobs_claimed=False,
                    require_training_handler=False,
                )

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), worker_app.handler_class(UnconfiguredRuntime())
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/ready", timeout=2)
            self.assertEqual(raised.exception.code, 503)
            payload = json.loads(raised.exception.read())
            self.assertEqual(payload["status"], "not_ready")
            self.assertFalse(payload["database"])
            self.assertFalse(payload["task_processing_enabled"])
            self.assertFalse(payload["training_jobs_claimed"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
