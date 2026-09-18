"""Request middleware: request ids, access logs, body-size limits.

A `request_id` is minted (or taken from an inbound `X-Request-ID`) and bound to
the logging context, so every log line emitted while serving that request —
including from deep inside a tool or the agent graph — carries it. That is what
makes "find everything that happened for this user's question" a single query.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.logging import bind_contextvars, clear_contextvars, get_logger
from app.observability.metrics import timing

log = get_logger("http")

#: Endpoints that should not spam the access log.
QUIET_PATHS = frozenset({"/api/v1/health", "/api/v1/ready", "/metrics", "/favicon.ico"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        clear_contextvars()
        bind_contextvars(request_id=request_id, path=request.url.path, method=request.method)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = int((time.perf_counter() - started) * 1000)
            log.exception("http.unhandled", duration_ms=elapsed)
            raise

        elapsed = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id

        if request.url.path not in QUIET_PATHS:
            log.info(
                "http.request",
                status_code=response.status_code,
                duration_ms=elapsed,
                client=request.client.host if request.client else None,
            )
            timing(
                "http.request_duration",
                elapsed,
                path=request.url.path,
                method=request.method,
                status=response.status_code,
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are read into memory.

    FastAPI will happily buffer a multi-gigabyte upload otherwise. Checking
    Content-Length is cheap and stops the obvious case; the streaming read in
    the upload route enforces the real limit for chunked requests.
    """

    def __init__(self, app: object, max_bytes: int | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        cfg = get_settings()
        self._max_bytes = max_bytes or (cfg.max_upload_mb * 1024 * 1024) + (1024 * 1024)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        raw_length = request.headers.get("content-length")
        if raw_length and raw_length.isdigit() and int(raw_length) > self._max_bytes:
            log.warning("http.body_too_large", content_length=int(raw_length))
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": f"Request body exceeds {self._max_bytes} bytes",
                        "retryable": False,
                        "details": {},
                    }
                },
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline response hardening."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        return response
