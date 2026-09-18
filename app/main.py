"""FastAPI application entrypoint.

Startup does four things and then gets out of the way:

1. configure structured logging and tracing;
2. refuse to boot a production environment with development secrets;
3. warm the database engine, Redis and the LangGraph checkpointer;
4. log which LLM provider is actually in use, so nobody debugs the offline
   provider for an hour thinking it is GPT.

Shutdown disposes every pool explicitly. A container that leaks connections on
SIGTERM exhausts RDS during a rolling deploy.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.errors import register_exception_handlers
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.observability.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.observability.tracing import init_tracing, instrument_fastapi

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
log = get_logger(__name__)

APP_VERSION = "0.1.0"

DESCRIPTION = """
An agentic research assistant for scientific literature and internal research data.

**What it does.** Classifies a question, plans typed tool calls, executes the
authorised ones (concurrently where they are independent), synthesises an answer
strictly from retrieved evidence, verifies that answer against the evidence, and
returns it with citations.

**Design principles it demonstrates**

* The LLM is not the application, and it is *not the security boundary*. Tool
  authorisation happens in backend code against the authenticated principal.
* Retrieved documents are untrusted data. They are delivered to the model inside
  labelled fences and can never become instructions.
* Agent autonomy is bounded by iteration, tool-call and wall-clock budgets.
* Sensitive actions pause for a human.

**Running without an API key.** With `OPENAI_API_KEY` unset the system uses a
deterministic offline provider, so every endpoint here works end to end — the
answers are extractive rather than generated.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agents.checkpointer import close_checkpointer, init_checkpointer
    from app.core.redis import close_redis, get_redis
    from app.database.session import check_database_ready, dispose_engines
    from app.llm.factory import close_llm_client, get_llm_client
    from app.tools.external.clinical_client import close_clinical_client

    init_tracing(settings)

    problems = settings.validate_for_environment()
    if problems:
        if settings.is_production:
            # Fail loudly rather than serve production traffic with dev secrets.
            log.error("startup.insecure_production_config", problems=problems)
            raise RuntimeError(f"Refusing to start in production: {problems}")
        log.warning("startup.development_config", problems=problems)

    db_ready = await check_database_ready()
    log.info("startup.database", ready=db_ready)

    try:
        await get_redis().ping()
        log.info("startup.redis", ready=True)
    except Exception as exc:  # noqa: BLE001 - Redis is degradable, not required
        log.warning("startup.redis_unavailable", error=str(exc))

    await init_checkpointer(settings)

    client = get_llm_client()
    log.info(
        "startup.complete",
        app=settings.app_name,
        version=APP_VERSION,
        environment=settings.environment,
        llm_provider=client.provider,
        reasoning_model=settings.llm_reasoning_model if settings.use_real_openai else "n/a",
        embedding_dim=settings.embedding_dim,
        mcp_enabled=settings.mcp_enabled,
    )

    yield

    log.info("shutdown.started")
    await close_clinical_client()
    await close_llm_client()
    await close_checkpointer()
    await close_redis()
    await dispose_engines()
    log.info("shutdown.complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Scientific Research Agent",
        description=DESCRIPTION,
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Scientific Research Agent"},
        license_info={"name": "MIT"},
        openapi_tags=[
            {"name": "ops", "description": "Liveness and readiness probes."},
            {"name": "auth", "description": "OAuth2/JWT authentication."},
            {"name": "users", "description": "Caller identity and permissions."},
            {"name": "agent", "description": "Chat, agent runs and approvals."},
            {"name": "documents", "description": "PDF upload and the ingestion pipeline."},
            {"name": "search", "description": "Direct literature and trial search."},
            {"name": "research", "description": "Experiments, compounds and topics."},
            {"name": "evaluation", "description": "Evaluation suite and results."},
        ],
    )

    # Middleware runs bottom-up: security headers wrap everything, then the
    # size limit rejects early, then request context binds the request id.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    instrument_fastapi(app, settings)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "app": settings.app_name,
            "version": APP_VERSION,
            "docs": "/docs",
            "health": f"{settings.api_v1_prefix}/health",
            "time": dt.datetime.now(dt.UTC).isoformat(),
        }

    return app


app = create_app()
