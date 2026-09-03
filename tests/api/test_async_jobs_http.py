from __future__ import annotations

import copy
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.async_runtime import DurableJobService, SqlAlchemyAsyncRepository
from apps.api.app.async_runtime.router import build_async_jobs_router
from apps.api.app.async_runtime.tables import AsyncRuntimeBase
from apps.api.app.auth import (
    AuthService,
    CookieSettings,
    JWTService,
    JWTSettings,
    PasswordService,
    build_auth_dependencies,
    build_auth_router,
    install_api_error_handlers,
)
from apps.api.app.auth.rate_limit import InMemoryRegistrationLimiter
from apps.api.app.db.models import Role
from apps.api.app.db.session import session_dependency

from ._support import NOW, PASSWORD, add_user, factory_for, sqlite_engine


def _payload() -> dict[str, object]:
    return {
        "idempotency_key": "async-http-json-contract",
        "task_name": "training",
        "payload": {"data_version": "immutable-v1"},
        "due_at": (NOW + timedelta(minutes=5)).isoformat(),
        "max_attempts": 3,
    }


def _login(client: TestClient, username: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    csrf = client.cookies.get("microlens_csrf")
    assert csrf
    return csrf


def test_async_job_accepts_json_datetimes_without_relaxing_numbers_or_rbac() -> None:
    engine = sqlite_engine()
    AsyncRuntimeBase.metadata.create_all(engine)
    sessions = factory_for(engine)
    with sessions.begin() as session:
        add_user(session, username="ops", role=Role.OPERATOR)
        add_user(session, username="readonly", role=Role.OPERATOR_READONLY)

    auth = AuthService(
        PasswordService(),
        JWTService(JWTSettings(secret="async-http-test-secret-longer-than-32-bytes")),
    )
    get_session = session_dependency(sessions)
    dependencies = build_auth_dependencies(get_session, auth)
    repository = SqlAlchemyAsyncRepository(sessions)
    jobs = DurableJobService(repository)
    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(
        build_auth_router(
            get_session=get_session,
            service=auth,
            dependencies=dependencies,
            limiter=InMemoryRegistrationLimiter(limit=10),
            cookies=CookieSettings(secure=False),
        )
    )
    app.include_router(
        build_async_jobs_router(
            dependencies=dependencies,
            jobs=jobs,
            repository=repository,
            allowed_task_names={"training"},
            clock=lambda: NOW,
        )
    )

    payload = _payload()
    with TestClient(app) as operator:
        csrf = _login(operator, "ops")
        assert operator.post("/api/admin/async-jobs", json=payload).status_code == 403

        created = operator.post(
            "/api/admin/async-jobs",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201, created.text
        document = created.json()
        assert document["created"] is True
        job_id = document["job"]["job_id"]
        assert document["job"]["due_at"] == (NOW + timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        )

        replay = operator.post(
            "/api/admin/async-jobs",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["created"] is False
        assert replay.json()["job"]["job_id"] == job_id

        for field, invalid in (("max_attempts", "3"), ("max_attempts", True)):
            invalid_payload = copy.deepcopy(payload)
            invalid_payload["idempotency_key"] = f"async-http-invalid-{type(invalid).__name__}"
            invalid_payload[field] = invalid
            response = operator.post(
                "/api/admin/async-jobs",
                json=invalid_payload,
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 422, response.text

        naive = copy.deepcopy(payload)
        naive["idempotency_key"] = "async-http-invalid-naive-time"
        naive["due_at"] = "2026-09-01T12:05:00"
        response = operator.post(
            "/api/admin/async-jobs",
            json=naive,
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 422, response.text

        claim = jobs.claim_next(worker_id="phase6-test", now=NOW + timedelta(minutes=5))
        assert claim is not None
        jobs.fail(
            claim,
            error=RuntimeError("injected failure"),
            retryable=False,
            retry_delay_seconds=0,
            now=NOW + timedelta(minutes=5, seconds=1),
        )
        retried = operator.post(
            f"/api/admin/async-jobs/{job_id}/retry",
            json={"due_at": (NOW + timedelta(minutes=7)).isoformat()},
            headers={"X-CSRF-Token": csrf},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["job"]["state"] == "queued"
        assert retried.json()["job"]["due_at"] == (NOW + timedelta(minutes=7)).isoformat().replace(
            "+00:00", "Z"
        )

    with TestClient(app) as readonly:
        csrf = _login(readonly, "readonly")
        denied = readonly.post(
            "/api/admin/async-jobs",
            json={**payload, "idempotency_key": "async-http-readonly-denied"},
            headers={"X-CSRF-Token": csrf},
        )
        assert denied.status_code == 403

    engine.dispose()
