"""Foundation worker process.

It exposes health/readiness only and deliberately does not claim, dequeue, or
execute training jobs. Phase 2D owns the real Redis-backed worker.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in {"/health", "/ready"}:
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "status": "ready" if self.path == "/ready" else "ok",
                "service": "worker",
                "phase": "foundation",
                "task_processing_enabled": False,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(json.dumps({"service": "worker", "message": format % args}))


def healthcheck() -> int:
    port = int(os.environ.get("WORKER_PORT", "8081"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=2) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def serve() -> None:
    port = int(os.environ.get("WORKER_PORT", "8081"))
    print(
        json.dumps(
            {
                "service": "worker",
                "event": "foundation_idle",
                "task_processing_enabled": False,
                "port": port,
            }
        )
    )
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        raise SystemExit(healthcheck())
    serve()
