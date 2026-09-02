from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.alerts.service import (
    AlertService,
    SqlAlchemyAlertRepository,
    WindowedMetricReader,
)
from apps.api.app.alerts.tables import AlertRuleRow
from apps.api.app.async_runtime.domain import require_aware
from apps.api.app.async_runtime.tables import AsyncJobRow
from apps.api.app.db.models import Event, EventType, RecommendationRequest
from apps.api.app.db.session import create_database_engine, create_session_factory
from apps.api.app.search.domain import READ_ALIAS
from apps.api.app.search.runtime import build_search_runtime
from apps.worker.async_tasks import RunOnceScheduler
from apps.worker.search_tasks import FullReindexTaskHandler, IncrementalIndexTaskHandler


class AlertEvaluationScheduler:
    """Evaluate enabled durable alert rules against an injected real metric reader."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        service: AlertService,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.sessions = sessions
        self.service = service
        self.clock = clock

    def run_once(self) -> dict[str, int]:
        now = require_aware(self.clock(), field="clock")
        with self.sessions() as session:
            rule_ids = list(
                session.scalars(
                    select(AlertRuleRow.rule_id)
                    .where(AlertRuleRow.enabled.is_(True))
                    .order_by(AlertRuleRow.rule_id)
                )
            )
        evaluated = 0
        failed = 0
        for rule_id in rule_ids:
            try:
                self.service.evaluate(rule_id, now=now)
            except Exception:
                # One malformed/unavailable metric must not starve other alert rules.
                failed += 1
            else:
                evaluated += 1
        return {"configured": len(rule_ids), "evaluated": evaluated, "failed": failed}


class ProductionScheduler:
    """Bounded durable async/alert pass with a graceful serve loop."""

    def __init__(
        self,
        durable: RunOnceScheduler,
        *,
        alerts: AlertEvaluationScheduler | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.durable = durable
        self.alerts = alerts
        self.clock = clock

    def run_once(self) -> dict[str, Any]:
        require_aware(self.clock(), field="clock")
        durable_result = self.durable.run_once()
        alert_result = (
            self.alerts.run_once()
            if self.alerts is not None
            else {"configured": 0, "evaluated": 0, "failed": 0}
        )
        return {"durable": durable_result, "alerts": alert_result}

    def serve(
        self,
        *,
        stop: threading.Event,
        poll_seconds: float = 2.0,
        on_result: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)
            else:
                if on_result is not None:
                    on_result(result)
            stop.wait(poll_seconds)


class SqlAlchemyMetricSampleSource:
    """Read alert samples from the existing PostgreSQL operational tables."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def samples(
        self,
        metric_name: str,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[float]:
        start = require_aware(window_start, field="window_start")
        end = require_aware(window_end, field="window_end")
        with self.sessions() as session:
            if metric_name == "recommendation_latency_ms":
                values = session.scalars(
                    select(RecommendationRequest.latency_ms).where(
                        RecommendationRequest.created_at >= start,
                        RecommendationRequest.created_at < end,
                        RecommendationRequest.latency_ms.is_not(None),
                    )
                )
                return [float(value) for value in values if value is not None]
            if metric_name == "recommendation_requests":
                count = session.scalar(
                    select(func.count())
                    .select_from(RecommendationRequest)
                    .where(
                        RecommendationRequest.created_at >= start,
                        RecommendationRequest.created_at < end,
                    )
                )
                return [1.0] * int(count or 0)
            if metric_name in {"events", "clicks"}:
                statement = (
                    select(func.count())
                    .select_from(Event)
                    .where(
                        Event.server_timestamp >= start,
                        Event.server_timestamp < end,
                    )
                )
                if metric_name == "clicks":
                    statement = statement.where(Event.event_type == EventType.CLICK)
                return [1.0] * int(session.scalar(statement) or 0)
            if metric_name == "async_queue_depth":
                count = session.scalar(
                    select(func.count())
                    .select_from(AsyncJobRow)
                    .where(AsyncJobRow.status == "queued")
                )
                return [float(count or 0)]
        raise ValueError(f"unsupported alert metric: {metric_name}")


@dataclass(frozen=True)
class SchedulerProcess:
    engine: Engine
    runtime: Any

    def run_once(self) -> dict[str, Any]:
        return self.runtime.run_once()

    def serve(self, *, stop: threading.Event, poll_seconds: float) -> None:
        self.runtime.scheduler.serve(
            stop=stop,
            poll_seconds=poll_seconds,
            on_result=lambda result: _log("scheduler_pass", result=result),
            on_error=lambda error: _log("scheduler_error", error_type=type(error).__name__),
        )

    def close(self) -> None:
        self.engine.dispose()


def build_scheduler_process() -> SchedulerProcess:
    # Imported here to avoid a module cycle: durable_runtime imports scheduler types.
    from apps.worker.durable_runtime import build_durable_worker_runtime

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    sessions = create_session_factory(engine)
    search = build_search_runtime(
        engine=engine,
        sessions=sessions,
        search_url=os.environ.get("SEARCH_URL", "http://search:9200"),
        search_read_alias=os.environ.get("SEARCH_READ_ALIAS", READ_ALIAS),
    )
    alert_repository = SqlAlchemyAlertRepository(sessions)
    alert_service = AlertService(
        alert_repository,
        WindowedMetricReader(SqlAlchemyMetricSampleSource(sessions)),
    )
    worker_id = os.environ.get("SCHEDULER_WORKER_ID") or (
        f"scheduler-{socket.gethostname()}-{os.getpid()}"
    )
    runtime = build_durable_worker_runtime(
        sessions,
        handlers=(
            FullReindexTaskHandler(search.full_reindexer),
            IncrementalIndexTaskHandler(search.incremental_indexer),
        ),
        worker_id=worker_id,
        redis_url=os.environ.get("REDIS_URL") or None,
        alert_service=alert_service,
        lease_seconds=_positive_int("SCHEDULER_LEASE_SECONDS", 60),
        retry_delay_seconds=_positive_int("SCHEDULER_RETRY_DELAY_SECONDS", 5),
    )
    return SchedulerProcess(engine=engine, runtime=runtime)


def main(
    argv: list[str] | None = None,
    *,
    process_factory: Callable[[], SchedulerProcess] = build_scheduler_process,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode = arguments[0] if arguments else "serve"
    if mode not in {"serve", "run-once"} or len(arguments) > 1:
        raise SystemExit("usage: python -m apps.worker.scheduler [serve|run-once]")
    process = process_factory()
    try:
        if mode == "run-once":
            print(json.dumps(process.run_once(), sort_keys=True, separators=(",", ":")))
            return 0
        stop = threading.Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
        _log("scheduler_started")
        process.serve(
            stop=stop,
            poll_seconds=float(os.environ.get("SCHEDULER_POLL_SECONDS", "2")),
        )
        return 0
    finally:
        process.close()


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _log(event: str, **fields: Any) -> None:
    print(
        json.dumps({"event": event, **fields}, sort_keys=True, separators=(",", ":")),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
