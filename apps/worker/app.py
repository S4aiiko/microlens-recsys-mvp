from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from apps.api.app.db.session import create_database_engine, create_session_factory

from .contracts import PermanentTrainingError, TrainingControl, TrainingRequest
from .jobs import JobCoordinator
from .operations import ScheduledOperationsRunner
from .redis_broker import RedisPyBroker
from .runtime import WorkerRuntime, structured_log, utc_now


class UnconfiguredTrainingHandler:
    def __call__(self, request: TrainingRequest, control: TrainingControl) -> dict[str, Any]:
        raise PermanentTrainingError("WORKER_TRAINING_HANDLER is not configured")


def load_training_handler(path: str):
    if ":" not in path:
        raise ValueError("WORKER_TRAINING_HANDLER must use module:attribute syntax")
    module_name, attribute_name = path.split(":", 1)
    handler = getattr(importlib.import_module(module_name), attribute_name)
    if isinstance(handler, type):
        handler = handler()
    if not callable(handler):
        raise TypeError("configured training handler is not callable")
    return handler


def parse_bool_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def build_runtime() -> WorkerRuntime:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)

    redis_url = os.environ.get("REDIS_URL", "")
    broker = RedisPyBroker.from_url(redis_url) if redis_url else None
    handler_path = os.environ.get("WORKER_TRAINING_HANDLER", "")
    handler = load_training_handler(handler_path) if handler_path else UnconfiguredTrainingHandler()
    worker_id = os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    coordinator = JobCoordinator(
        factory,
        broker=broker,
        lease_seconds=int(os.environ.get("WORKER_LEASE_SECONDS", "60")),
        max_attempts=int(os.environ.get("WORKER_MAX_ATTEMPTS", "3")),
    )
    operations = ScheduledOperationsRunner(factory, clock=utc_now)
    return WorkerRuntime(
        coordinator=coordinator,
        handler=handler,
        worker_id=worker_id,
        scheduled_operations=operations,
        handler_configured=bool(handler_path),
        require_training_handler=parse_bool_env("WORKER_REQUIRE_TRAINING_HANDLER", default=False),
    )


def handler_class(runtime: WorkerRuntime):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path not in {"/health", "/ready"}:
                self.send_error(404)
                return
            if self.path == "/health":
                payload: dict[str, Any] = {"status": "ok", "service": "worker"}
                status = 200
            else:
                readiness = runtime.readiness()
                payload = {"service": "worker", **readiness.as_dict()}
                status = 200 if readiness.ready else 503
            encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            structured_log("http_access", message=format % args)

    return Handler


def healthcheck() -> int:
    port = int(os.environ.get("WORKER_PORT", "8081"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=2) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def serve(runtime: WorkerRuntime) -> None:
    port = int(os.environ.get("WORKER_PORT", "8081"))
    poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
    stop = threading.Event()

    def work_loop() -> None:
        while not stop.is_set():
            try:
                runtime.run_once()
            except Exception as exc:
                structured_log("worker_loop_error", error_type=type(exc).__name__)
            stop.wait(poll_seconds)

    thread = threading.Thread(target=work_loop, name="training-worker", daemon=True)
    thread.start()
    structured_log("worker_started", port=port, worker_id=runtime.worker_id)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_class(runtime))
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--healthcheck"]:
        return healthcheck()
    if arguments == ["durable-run-once"]:
        # Durable/search jobs have a separate PostgreSQL-authoritative process.
        # Keeping this explicit preserves the existing training job state machine.
        from .scheduler import main as scheduler_main

        return scheduler_main(["run-once"])
    runtime = build_runtime()
    if arguments == ["run-once"]:
        result = runtime.run_once()
        print(json.dumps(result, sort_keys=True))
        return 2 if result["job"] == "training_handler_unconfigured" else 0
    if arguments:
        raise SystemExit(
            "usage: python -m apps.worker.app [run-once|durable-run-once|--healthcheck]"
        )
    serve(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
