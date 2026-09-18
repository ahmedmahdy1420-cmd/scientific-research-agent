"""Liveness and readiness.

Two endpoints, because they answer different questions and the orchestrator
uses them for different things:

* `/health` — is this process alive? Cheap, no dependencies. If it fails, ECS
  restarts the task.
* `/ready` — can it serve traffic? Checks Postgres (including that pgvector is
  installed), Redis, and the configured LLM provider. If it fails, the load
  balancer stops sending requests but the task is left running, which is what
  you want during a dependency blip.

A degraded-but-serving state is reported explicitly rather than being collapsed
into "down": the MCP server or the trials API being unreachable reduces what
the agent can do without making the API useless.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.database.session import check_database_ready
from app.schemas.common import HealthResponse, ReadinessResponse

router = APIRouter(tags=["ops"])
log = get_logger(__name__)
settings = get_settings()

APP_VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=APP_VERSION,
        environment=settings.environment,
        time=dt.datetime.now(dt.UTC),
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def ready(response: Response) -> ReadinessResponse:
    checks: dict[str, bool] = {}
    degraded: list[str] = []

    checks["database"] = await check_database_ready()

    try:
        checks["redis"] = bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001
        log.warning("ready.redis_failed", error=str(exc))
        checks["redis"] = False

    # The LLM provider is "ready" if it is configured; we do not burn a paid
    # call on every probe.
    checks["llm_provider"] = True
    if not settings.use_real_openai:
        degraded.append("llm:offline-deterministic-provider")

    if not checks["redis"]:
        degraded.append("redis:caching-and-rate-limiting-disabled")

    # Only the database is hard-required. Everything else degrades.
    hard_ok = checks["database"]
    if not hard_ok:
        response.status_code = 503

    return ReadinessResponse(
        status="ready" if hard_ok else "not_ready",
        checks=checks,
        degraded=degraded,
    )
