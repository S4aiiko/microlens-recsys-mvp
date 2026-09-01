from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str | None
    details: dict[str, Any] | list[Any] | None


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str):
        return value
    header = request.headers.get("X-Request-ID")
    return header if header else None


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    envelope = ErrorEnvelope(
        code=exc.code,
        message=exc.message,
        request_id=request_id(request),
        details=exc.details,
    )
    return JSONResponse(status_code=exc.status_code, content=envelope.model_dump(mode="json"))


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {key: value for key, value in error.items() if key not in {"ctx", "input", "url"}}
        for error in exc.errors()
    ]
    envelope = ErrorEnvelope(
        code="validation_error",
        message="Request validation failed",
        request_id=request_id(request),
        details=details,
    )
    return JSONResponse(status_code=422, content=envelope.model_dump(mode="json"))


def install_api_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
