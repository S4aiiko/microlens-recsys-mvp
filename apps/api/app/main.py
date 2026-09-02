from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.api.app.alerts.router import build_alerts_router
from apps.api.app.alerts.service import SqlAlchemyAlertRepository
from apps.api.app.api.admin.queries import DashboardQueryService
from apps.api.app.api.admin.router import build_dashboard_router
from apps.api.app.async_runtime.router import build_async_jobs_router
from apps.api.app.async_runtime.runtime import create_async_runtime
from apps.api.app.auth.dependencies import build_auth_dependencies
from apps.api.app.auth.errors import ErrorEnvelope, install_api_error_handlers
from apps.api.app.auth.rate_limit import RedisRegistrationLimiter
from apps.api.app.auth.router import build_auth_router, build_role_admin_router
from apps.api.app.auth.security import CookieSettings, JWTService, JWTSettings, PasswordService
from apps.api.app.auth.service import AuthService
from apps.api.app.db.session import session_dependency
from apps.api.app.events.router import build_events_router
from apps.api.app.events.service import EventService
from apps.api.app.feeds.cursor import CursorCodec
from apps.api.app.feeds.resources import derive_feed_cursor_secret
from apps.api.app.feeds.router import build_feeds_router
from apps.api.app.feeds.service import RecommendationService
from apps.api.app.models_registry.repository import ModelRegistryRepository
from apps.api.app.models_registry.router import build_model_admin_router
from apps.api.app.operation_jobs.router import build_operation_jobs_router
from apps.api.app.operation_jobs.service import OperationJobService
from apps.api.app.operations.router import build_items_router, build_operations_router
from apps.api.app.operations.service import OperationService
from apps.api.app.runtime import RuntimeContext, create_runtime
from apps.api.app.search.router import build_search_router
from apps.api.app.search.runtime import SearchRuntime, build_search_runtime
from apps.api.app.settings import AppSettings


class Health(BaseModel):
    status: str
    service: str
    phase: str


class Ready(BaseModel):
    status: str
    service: str
    phase: str
    checks: dict[str, object]
    business_routes_ready: bool


def _error(status_code: int, code: str, message: str, request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    envelope = ErrorEnvelope(code=code, message=message, request_id=request_id, details=None)
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


def _install_openapi_metadata(app: FastAPI) -> None:
    original: Callable[[], dict[str, Any]] = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original()
        schema["servers"] = [
            {"url": "http://localhost:8000", "description": "Host-published public listener"}
        ]
        schema["x-listener-contract"] = {
            "public": {
                "url": "http://api:8000",
                "host_published": True,
                "reject_path_prefixes": ["/internal/"],
            },
            "internal_activation": {
                "contract": "internal-openapi.json",
                "url": "http://api:8001",
                "host_published": False,
            },
        }
        schema["info"]["x-implementation-status"] = "phase_4_feed_runtime"
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes.update(
            {
                "cookieAuth": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "microlens_session",
                },
                "csrfHeader": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-CSRF-Token",
                },
            }
        )
        components = schema["components"]
        components.setdefault("responses", {})["Error"] = {
            "description": "Structured API error",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}
            },
        }
        renames = {
            "AdminItemResponse": "AdminItem",
            "AuditOperationResponse": "AuditOperation",
            "PersistedEventResponse": "PersistedEvent",
            "UserResponse": "User",
        }

        def update_references(value: object) -> None:
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str):
                    for old, new in renames.items():
                        if reference == f"#/components/schemas/{old}":
                            value["$ref"] = f"#/components/schemas/{new}"
                for nested in value.values():
                    update_references(nested)
            elif isinstance(value, list):
                for nested in value:
                    update_references(nested)

        update_references(schema)
        schemas = components.setdefault("schemas", {})
        for old, new in renames.items():
            if old in schemas:
                schemas[new] = schemas.pop(old)
                schemas[new]["title"] = new
        schemas["ServerEventType"] = {
            "type": "string",
            "enum": [
                "impression",
                "click",
                "like",
                "not_interested",
                "dwell",
                "revisit",
                "share",
            ],
            "title": "ServerEventType",
        }
        schemas["ClientEventType"] = {
            "type": "string",
            "enum": ["click", "like", "not_interested", "dwell", "revisit", "share"],
            "title": "ClientEventType",
        }
        if "EventRequest" in schemas:
            schemas["EventRequest"]["properties"]["event_type"] = {
                "$ref": "#/components/schemas/ClientEventType"
            }
        if "PersistedEvent" in schemas:
            schemas["PersistedEvent"]["properties"]["event_type"] = {
                "$ref": "#/components/schemas/ServerEventType"
            }
            schemas["PersistedEvent"]["required"] = [
                field
                for field in schemas["PersistedEvent"].get("required", [])
                if field not in {"duration_ms", "payload"}
            ]
        csv_success = schema["paths"]["/api/admin/dashboard/export.csv"]["get"]["responses"]["200"]
        csv_success["content"] = {"text/csv": {"schema": {"type": "string", "format": "binary"}}}
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                if not isinstance(operation, dict) or not operation.get("security"):
                    continue
                operation.setdefault("responses", {}).setdefault(
                    "401", {"$ref": "#/components/responses/Error"}
                )
                operation["responses"].setdefault("403", {"$ref": "#/components/responses/Error"})
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


def create_public_app(
    settings: AppSettings | None = None,
    runtime: RuntimeContext | None = None,
    search_runtime: SearchRuntime | None = None,
) -> FastAPI:
    settings = settings or AppSettings.from_environment(allow_unconfigured=True)
    runtime = runtime or create_runtime(settings)
    app = FastAPI(
        title="MicroLens Recommendation MVP API",
        version="0.4.0-phase4",
        description="Public runtime with Phase 4 online recommendation orchestration.",
    )
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    @app.middleware("http")
    async def public_boundary(request: Request, call_next: Callable[..., Any]) -> Any:
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        if request.url.path == "/internal" or request.url.path.startswith("/internal/"):
            response = _error(404, "not_found", "Route does not exist", request)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    install_api_error_handlers(app)

    @app.exception_handler(FastAPIHTTPException)
    async def http_error(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
        message = str(exc.detail) if isinstance(exc.detail, str) else "HTTP request failed"
        return _error(exc.status_code, "http_error", message, request)

    get_session = session_dependency(runtime.sessions)
    password_service = PasswordService()
    auth_service = AuthService(
        password_service,
        JWTService(JWTSettings(secret=settings.jwt_secret, lifetime=settings.session_lifetime)),
    )
    auth_dependencies = build_auth_dependencies(get_session, auth_service)
    recommendation_service = RecommendationService(
        model_provider=runtime.model_slot.snapshot,
        cache=runtime.recommendation_cache,
        cursor_codec=CursorCodec(derive_feed_cursor_secret(settings.jwt_secret)),
    )
    async_runtime = create_async_runtime(
        runtime.sessions,
        redis_url=settings.redis_url if settings.configured else None,
    )
    search_runtime = search_runtime or build_search_runtime(
        engine=runtime.engine,
        sessions=runtime.sessions,
        search_url=settings.search_url,
        search_read_alias=settings.search_read_alias,
    )
    if settings.configured and runtime.redis is not None:
        limiter = RedisRegistrationLimiter(
            runtime.redis,
            limit=settings.registration_limit,
            window_seconds=settings.registration_window_seconds,
        )
    else:
        # Reachable only in dependency-light, explicitly unconfigured contract tests.
        from apps.api.app.auth.rate_limit import InMemoryRegistrationLimiter

        limiter = InMemoryRegistrationLimiter(limit=settings.registration_limit)
    cookies = CookieSettings(
        secure=settings.cookie_secure,
        same_site=settings.cookie_same_site,  # type: ignore[arg-type]
        domain=settings.cookie_domain,
    )
    app.include_router(
        build_auth_router(
            get_session=get_session,
            service=auth_service,
            dependencies=auth_dependencies,
            limiter=limiter,
            cookies=cookies,
        )
    )
    app.include_router(
        build_role_admin_router(get_session=get_session, dependencies=auth_dependencies)
    )
    app.include_router(
        build_events_router(
            get_session=get_session,
            dependencies=auth_dependencies,
            service=EventService(),
        )
    )
    app.include_router(
        build_feeds_router(
            get_session=get_session,
            dependencies=auth_dependencies,
            service=recommendation_service,
        )
    )
    # Static search must precede the dynamic /api/items/{item_id} route.
    app.include_router(
        build_search_router(
            dependencies=auth_dependencies,
            service=search_runtime.service,
            health_service=search_runtime.health_service,
        )
    )
    app.include_router(build_items_router(get_session=get_session, dependencies=auth_dependencies))
    app.include_router(
        build_operations_router(
            get_session=get_session,
            dependencies=auth_dependencies,
            service=OperationService(),
        )
    )
    app.include_router(
        build_async_jobs_router(
            dependencies=auth_dependencies,
            jobs=async_runtime.jobs,
            repository=async_runtime.repository,
            allowed_task_names={"search.full_reindex", "search.incremental_index"},
        )
    )
    app.include_router(
        build_operation_jobs_router(
            dependencies=auth_dependencies,
            service=OperationJobService(async_runtime.jobs, async_runtime.repository),
            repository=async_runtime.repository,
        )
    )
    app.include_router(
        build_alerts_router(
            dependencies=auth_dependencies,
            sessions=runtime.sessions,
            repository=SqlAlchemyAlertRepository(runtime.sessions),
        )
    )
    app.include_router(
        build_dashboard_router(
            get_session=get_session,
            dependencies=auth_dependencies,
            queries=DashboardQueryService(),
        )
    )
    app.include_router(
        build_model_admin_router(
            get_session=get_session,
            dependencies=auth_dependencies,
            repository=ModelRegistryRepository(),
        )
    )

    @app.get("/health", response_model=Health, tags=["system"], operation_id="getHealth")
    def health() -> Health:
        return Health(status="ok", service="api", phase="phase_4")

    @app.get("/ready", response_model=Ready, tags=["system"], operation_id="getReady")
    async def ready() -> JSONResponse:
        is_ready, checks = runtime.readiness()
        if settings.configured:
            try:
                redis_ready = await runtime.ping_redis()
            except Exception as exc:
                redis_ready = False
                checks["redis_error"] = type(exc).__name__
            checks["redis"] = "ok" if redis_ready else "unavailable"
            is_ready = is_ready and redis_ready
        payload = {
            "status": "ready" if is_ready else "not_ready",
            "service": "api",
            "phase": "phase_4",
            "checks": checks,
            "business_routes_ready": is_ready,
        }
        status_code = 200 if is_ready or not settings.configured else 503
        return JSONResponse(status_code=status_code, content=payload)

    _install_openapi_metadata(app)
    return app


app = create_public_app()
