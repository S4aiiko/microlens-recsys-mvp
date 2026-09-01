from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request

from apps.api.app.auth.errors import install_api_error_handlers
from apps.api.app.db.session import session_dependency
from apps.api.app.models_registry.router import build_internal_activation_router
from apps.api.app.models_registry.service import ActivationService
from apps.api.app.runtime import RuntimeContext, SecureJsonStagingLoader, create_runtime
from apps.api.app.settings import AppSettings

LOGGER = logging.getLogger("microlens.security.internal_activation")


def _install_internal_openapi_metadata(app: FastAPI) -> None:
    original = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original()
        schema["servers"] = [
            {
                "url": "http://api:8001",
                "description": "Non-host-published Compose backend listener",
            }
        ]
        schema["x-listener-contract"] = {
            "host_published": False,
            "compose_network": "backend",
            "network_scope": "compose-internal-only",
            "public_listener_rejects": ["/internal/"],
        }
        schema["info"]["x-implementation-status"] = "phase_2d_internal_runtime"
        schema.setdefault("components", {}).setdefault("securitySchemes", {})[
            "publishToken"
        ] = {"type": "apiKey", "in": "header", "name": "X-Publish-Token"}
        components = schema["components"]
        components.setdefault("responses", {})["Error"] = {
            "description": "Internal publishing error",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}
            },
        }
        schemas = components.setdefault("schemas", {})
        schemas.setdefault(
            "ErrorEnvelope",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "message", "request_id", "details"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "request_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "details": {},
                },
            },
        )
        if "ModelVersionResponse" in schemas:
            schemas["ModelVersion"] = schemas.pop("ModelVersionResponse")
            schemas["ModelVersion"]["title"] = "ModelVersion"
            operation = schema["paths"]["/internal/model-versions/{version}/activate"]["post"]
            success = operation["responses"]["200"]["content"]["application/json"]["schema"]
            success["$ref"] = "#/components/schemas/ModelVersion"
        operation = schema["paths"]["/internal/model-versions/{version}/activate"]["post"]
        for status_code in ("401", "409", "422"):
            operation.setdefault("responses", {})[status_code] = {
                "$ref": "#/components/responses/Error"
            }
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


def create_internal_app(
    settings: AppSettings | None = None,
    runtime: RuntimeContext | None = None,
) -> FastAPI:
    settings = settings or AppSettings.from_environment(allow_unconfigured=True)
    runtime = runtime or create_runtime(settings)
    app = FastAPI(
        title="MicroLens Internal Model Activation API",
        version="0.2.0-phase2d",
        description="Compose-internal publishing runtime; never host-published.",
    )
    app.state.runtime = runtime
    app.state.last_invalid_token_log = 0.0
    install_api_error_handlers(app)

    @app.middleware("http")
    async def security_boundary(request: Request, call_next: Callable[..., Any]) -> Any:
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        response = await call_next(request)
        if response.status_code == 401:
            now = time.monotonic()
            if now - app.state.last_invalid_token_log >= 10.0:
                LOGGER.warning(
                    "internal activation authentication rejected",
                    extra={"request_id": request.state.request_id},
                )
                app.state.last_invalid_token_log = now
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    service = ActivationService(
        publish_token=settings.publish_token,
        loader=SecureJsonStagingLoader(settings.model_artifacts_dir),
    )
    app.include_router(
        build_internal_activation_router(
            get_session=session_dependency(runtime.sessions),
            service=service,
            runtime=runtime.model_slot,
        )
    )
    _install_internal_openapi_metadata(app)
    return app


app = create_internal_app()
