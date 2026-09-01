from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import urllib.request

try:
    from fastapi.testclient import TestClient

    from apps.api.app.main import app
except ModuleNotFoundError:
    TestClient = None
    app = None


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
            self.assertFalse(ready.json()["business_routes_ready"])


class WorkerHealthTest(unittest.TestCase):
    def test_worker_health_surface_is_explicitly_idle(self) -> None:
        environment = os.environ.copy()
        environment["WORKER_PORT"] = "18081"
        process = subprocess.Popen(
            [sys.executable, "-m", "apps.worker.app"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        try:
            for _ in range(40):
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:18081/ready", timeout=0.2
                    ) as response:
                        payload = response.read().decode("utf-8")
                        self.assertEqual(response.status, 200)
                        self.assertIn('"task_processing_enabled": false', payload)
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("worker readiness endpoint did not start")
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
