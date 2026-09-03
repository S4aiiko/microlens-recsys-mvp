from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "microlens_foundation_under_test", ROOT / "scripts" / "foundation.py"
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard.
    raise RuntimeError("Unable to load scripts/foundation.py")
foundation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(foundation)


class DockerDoctorTest(unittest.TestCase):
    @patch.object(foundation.shutil, "which", return_value=None)
    def test_cli_missing_is_distinct(self, _which: object) -> None:
        checks, status = foundation.docker_preflight()
        self.assertEqual(checks["docker_cli"], "MISSING")
        self.assertEqual(checks["docker_compose"], "NOT_CHECKED")
        self.assertEqual(status, "NOT_READY_DOCKER_CLI_MISSING")

    @patch.object(foundation.subprocess, "run")
    @patch.object(foundation.shutil, "which", return_value="/test/bin/docker")
    def test_compose_missing_is_distinct(self, _which: object, run: object) -> None:
        run.return_value = subprocess.CompletedProcess([], 1)  # type: ignore[attr-defined]
        checks, status = foundation.docker_preflight()
        self.assertEqual(checks["docker_compose"], "UNAVAILABLE")
        self.assertEqual(checks["docker_daemon"], "NOT_CHECKED")
        self.assertEqual(status, "NOT_READY_DOCKER_COMPOSE_MISSING")

    @patch.object(foundation.subprocess, "run")
    @patch.object(foundation.shutil, "which", return_value="/test/bin/docker")
    def test_daemon_unavailable_is_distinct(self, _which: object, run: object) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 1),
        ]
        checks, status = foundation.docker_preflight()
        self.assertEqual(checks["docker_compose"], "AVAILABLE")
        self.assertEqual(checks["docker_daemon"], "UNAVAILABLE")
        self.assertEqual(status, "NOT_READY_DOCKER_DAEMON_UNAVAILABLE")

    @patch.object(foundation.subprocess, "run")
    @patch.object(foundation.shutil, "which", return_value="/test/bin/docker")
    def test_success_requires_compose_and_daemon(self, _which: object, run: object) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]
        checks, status = foundation.docker_preflight()
        self.assertEqual(checks["docker_compose"], "AVAILABLE")
        self.assertEqual(checks["docker_daemon"], "AVAILABLE")
        self.assertEqual(status, "FOUNDATION_PREFLIGHT_READY")
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], foundation.DOCKER_CHECK_TIMEOUT_SECONDS)
            self.assertFalse(call.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
