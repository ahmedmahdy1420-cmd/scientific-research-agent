"""Exception handlers.

Every error leaves the API in one shape:

    {"error": {"code", "message", "retryable", "details"}, "request_id": "..."}

so a client can branch on `code` rather than parsing prose, and `retryable`
tells it whether trying again could possibly help.

Unhandled exceptions are logged with a stack trace and returned as a generic
500. The internal message is never echoed to the client: exception text leaks
table names, file paths and occasionally credentials.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger(__name__)


def _payload(
    code: str,
    message: str,
    request: Request,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        },
        "request_id": getattr(request.state, "request_id", None),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        level = log.warning if exc.http_status < 500 else log.error
        level(
            "api.app_error",
            code=exc.code,
            status=exc.http_status,
            message=exc.message,
            path=request.url.path,
        )
        headers = {}
        if exc.http_status == 429:
            retry_after = exc.details.get("retry_after_seconds")
            if retry_after:
                headers["Retry-After"] = str(retry_after)
        if exc.http_status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(
                exc.code, exc.message, request, retryable=exc.retryable, details=exc.details
            ),
            headers=headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", [])[1:]) or "body",
                "message": err.get("msg", "invalid"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()[:10]
        ]
        log.info("api.validation_error", path=request.url.path, errors=errors)
        return JSONResponse(
            status_code=422,
            content=_payload(
                "validation_error",
                "Request validation failed",
                request,
                details={"errors": errors},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "authentication_failed", 403: "authorization_failed", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code, str(exc.detail), request),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception(
            "api.unhandled_error",
            path=request.url.path,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=_payload(
                "internal_error",
                "An unexpected error occurred. The incident has been logged.",
                request,
            ),
        )
