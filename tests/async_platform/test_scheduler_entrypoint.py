from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stdout

from apps.worker.scheduler import main


class FakeSchedulerProcess:
    def __init__(self) -> None:
        self.closed = False
        self.served = False

    def run_once(self) -> dict[str, object]:
        return {"durable": {"job": "idle"}, "alerts": {"evaluated": 0}}

    def serve(self, *, stop: threading.Event, poll_seconds: float) -> None:
        self.served = True
        self.poll_seconds = poll_seconds
        stop.set()

    def close(self) -> None:
        self.closed = True


class SchedulerEntrypointTests(unittest.TestCase):
    def test_run_once_prints_json_and_closes_runtime(self) -> None:
        process = FakeSchedulerProcess()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["run-once"], process_factory=lambda: process)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["durable"]["job"], "idle")
        self.assertTrue(process.closed)

    def test_serve_is_a_long_running_mode_with_graceful_close(self) -> None:
        process = FakeSchedulerProcess()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["serve"], process_factory=lambda: process)
        self.assertEqual(code, 0)
        self.assertTrue(process.served)
        self.assertTrue(process.closed)
        self.assertGreater(process.poll_seconds, 0)


if __name__ == "__main__":
    unittest.main()
