from __future__ import annotations

import copy
import uuid
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.async_runtime import DurableJobService, SqlAlchemyAsyncRepository
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
from apps.api.app.operation_jobs import OperationJobService
from apps.api.app.operation_jobs.router import build_operation_jobs_router

from ._support import NOW, PASSWORD, add_user, factory_for, sqlite_engine


def _payload() -> dict[str, object]:
    operation_id = str(uuid.uuid4())
    return {
        "operation_id": operation_id,
        "idempotency_key": f"operation-http-{operation_id}",
        "kind": "promote",
        "targets": [{"target_id": "item-a", "state_version": 1}],
        "due_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ends_at_utc": (NOW + timedelta(minutes=10)).isoformat(),
        "scope_type": "feed",
        "scope_value": "popular",
        "priority": 5,
        "target_position": 0,
        "reason": "HTTP JSON contract regression",
        "max_attempts": 3,
    }


def _login(client: TestClient, username: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    csrf = client.cookies.get("microlens_csrf")
    assert csrf
    return csrf


def test_operation_job_accepts_openapi_json_and_preserves_strict_fields_and_rbac() -> None:
    engine = sqlite_engine()
    AsyncRuntimeBase.metadata.create_all(engine)
    sessions = factory_for(engine)
    with sessions.begin() as session:
        add_user(session, username="ops", role=Role.OPERATOR)
        add_user(session, username="readonly", role=Role.OPERATOR_READONLY)

    auth = AuthService(
        PasswordService(),
        JWTService(JWTSettings(secret="operation-http-test-secret-longer-than-32-bytes")),
    )
    get_session = session_dependency(sessions)
    dependencies = build_auth_dependencies(get_session, auth)
    repository = SqlAlchemyAsyncRepository(sessions)
    service = OperationJobService(DurableJobService(repository), repository)
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
        build_operation_jobs_router(
            dependencies=dependencies,
            service=service,
            repository=repository,
            clock=lambda: NOW,
        )
    )

    payload = _payload()
    operation_id = uuid.UUID(str(payload["operation_id"]))
    with TestClient(app) as operator:
        csrf = _login(operator, "ops")
        missing_csrf = operator.post("/api/admin/operation-jobs", json=payload)
        assert missing_csrf.status_code == 403

        created = operator.post(
            "/api/admin/operation-jobs",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201, created.text
        assert created.json()["created"] is True
        assert created.json()["job"]["job_id"] == str(operation_id)
        assert repository.get(operation_id) is not None

        replay = operator.post(
            "/api/admin/operation-jobs",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["created"] is False
        assert replay.json()["job"]["job_id"] == str(operation_id)

        invalid_values = [
            ("state_version_string", ("targets", 0, "state_version"), "1"),
            ("state_version_boolean", ("targets", 0, "state_version"), True),
            ("priority_string", ("priority",), "5"),
            ("max_attempts_string", ("max_attempts",), "3"),
            ("naive_due_at", ("due_at",), "2026-09-01T12:05:00"),
        ]
        for label, path, invalid_value in invalid_values:
            invalid = copy.deepcopy(payload)
            invalid_id = str(uuid.uuid4())
            invalid["operation_id"] = invalid_id
            invalid["idempotency_key"] = f"operation-http-invalid-{label}-{invalid_id}"
            target: object = invalid
            for part in path[:-1]:
                target = target[part]  # type: ignore[index]
            target[path[-1]] = invalid_value  # type: ignore[index]
            response = operator.post(
                "/api/admin/operation-jobs",
                json=invalid,
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 422, (label, response.text)
            assert repository.get(uuid.UUID(invalid_id)) is None

    with TestClient(app) as readonly:
        csrf = _login(readonly, "readonly")
        denied = readonly.post(
            "/api/admin/operation-jobs",
            json=_payload(),
            headers={"X-CSRF-Token": csrf},
        )
        assert denied.status_code == 403

    engine.dispose()
