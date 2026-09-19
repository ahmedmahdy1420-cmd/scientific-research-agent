"""MCP server exposing this system's research data as standard tools.

Why MCP is here at all
----------------------
The agent in this repo could call `app/tools/sql_tools.py` directly — and for
its own tools it does. MCP earns its place when the *consumer* is not this
codebase: Claude Desktop, an IDE, a colleague's agent, or a second internal
service. Without it, each of those needs a bespoke integration against our REST
API plus its own auth handling. With it, they speak one protocol, discover the
tools and their JSON Schemas at runtime, and get typed results back.

So the rule this file follows: **MCP is a transport, not a second
implementation.** Each tool below is a thin adapter over the same service layer
the REST API uses. There is no business logic here that exists nowhere else.

Security
--------
An MCP server is a remotely callable surface, so it is authenticated. Transport
is streamable HTTP behind a bearer-token middleware (`MCP_AUTH_TOKEN`), and the
server resolves that token to a *service principal* with a fixed, restricted
permission set — it deliberately cannot read restricted documents and cannot
touch the sensitive-action tool. A leaked MCP token is therefore limited to the
public/internal corpus.

Run it:
    python -m app.mcp --transport streamable-http     # http://localhost:8020/mcp
    python -m app.mcp --transport stdio               # for Claude Desktop
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from app.auth.permissions import Permission, Principal
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import get_async_session_factory
from app.llm.factory import get_llm_client
from app.rag.embeddings import LLMEmbeddingProvider
from app.rag.pipeline import RAGPipeline
from app.rag.retriever import RetrievalFilters, Retriever
from app.services.research_service import ResearchService
from app.tools.external.clinical_client import get_clinical_client

log = get_logger(__name__)
settings = get_settings()

#: The identity every MCP call runs as. Note what it does NOT have:
#: documents:read_restricted, experiments:read_restricted, tools:sensitive.
MCP_SERVICE_PRINCIPAL = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    email="mcp-service@internal",
    full_name="MCP service account",
    roles=frozenset({"service"}),
    permissions=frozenset(
        {
            Permission.DOCUMENTS_READ,
            Permission.SEARCH_LITERATURE,
            Permission.SEARCH_TRIALS,
            Permission.EXPERIMENTS_READ,
            Permission.COMPOUNDS_READ,
        }
    ),
)

server = MCPServer(
    name="fahem",
    version="0.1.0",
    instructions=(
        "Scientific research data for an internal corpus. Tools cover document "
        "search (semantic), experiments, compounds and clinical trials. All "
        "results are filtered to non-restricted material. Cite the returned "
        "source_id values; do not infer facts that are not in the results."
    ),
)


# =============================================================================
# Output schemas - MCP advertises these, so clients get typed results
# =============================================================================
class DocumentHit(BaseModel):
    source_id: str = Field(description="Citable identifier for this passage")
    title: str
    snippet: str
    relevance: float | None = None
    publication_date: dt.date | None = None
    research_area: str | None = None


class ResearchSearchResult(BaseModel):
    query: str
    entity_type: str
    count: int
    documents: list[DocumentHit] = Field(default_factory=list)
    experiments: list[dict[str, Any]] = Field(default_factory=list)
    compounds: list[dict[str, Any]] = Field(default_factory=list)


class ExperimentDetail(BaseModel):
    found: bool
    code: str | None = None
    title: str | None = None
    hypothesis: str | None = None
    methodology: str | None = None
    status: str | None = None
    model_system: str | None = None
    sample_size: int | None = None
    compound: str | None = None
    results: list[dict[str, Any]] = Field(default_factory=list)


class LiteratureResult(BaseModel):
    query: str
    count: int
    passages: list[DocumentHit] = Field(default_factory=list)


class TrialInformation(BaseModel):
    found: bool
    registry_id: str
    title: str | None = None
    condition: str | None = None
    intervention: str | None = None
    phase: str | None = None
    status: str | None = None
    sponsor: str | None = None
    enrollment: int | None = None
    primary_outcome: str | None = None
    summary: str | None = None
    source: str = "external_api"


# =============================================================================
# Tools
# =============================================================================
@server.tool(
    name="search_research_data",
    title="Search research data",
    description=(
        "Cross-entity search over the research corpus. entity_type selects "
        "documents (semantic passage search), experiments, compounds, or all."
    ),
)
async def search_research_data(
    query: Annotated[str, Field(min_length=2, max_length=500, description="Search text")],
    entity_type: Annotated[
        Literal["all", "documents", "experiments", "compounds"],
        Field(description="Which entity kind to search"),
    ] = "all",
    limit: Annotated[int, Field(ge=1, le=25, description="Max results per entity kind")] = 8,
) -> ResearchSearchResult:
    factory = get_async_session_factory()
    documents: list[DocumentHit] = []
    experiments: list[dict[str, Any]] = []
    compounds: list[dict[str, Any]] = []

    async with factory() as session:
        if entity_type in ("all", "documents"):
            pipeline = RAGPipeline(Retriever(session, LLMEmbeddingProvider(get_llm_client())))
            context = await pipeline.run(
                query,
                MCP_SERVICE_PRINCIPAL,
                filters=RetrievalFilters(),
                top_k=limit,
            )
            documents = [
                DocumentHit(
                    source_id=item.source_id,
                    title=item.title,
                    snippet=item.snippet[:800],
                    relevance=item.relevance_score,
                    publication_date=item.publication_date,
                )
                for item in context.evidence
            ]

        service = ResearchService(session)
        if entity_type in ("all", "experiments"):
            experiments = await service.search_experiments_summary(
                query, MCP_SERVICE_PRINCIPAL, limit=limit
            )
        if entity_type in ("all", "compounds"):
            compounds = await service.search_compounds_summary(query, limit=limit)

    total = len(documents) + len(experiments) + len(compounds)
    log.info("mcp.tool", tool="search_research_data", entity_type=entity_type, count=total)
    return ResearchSearchResult(
        query=query,
        entity_type=entity_type,
        count=total,
        documents=documents,
        experiments=experiments,
        compounds=compounds,
    )


@server.tool(
    name="get_experiment",
    title="Get experiment",
    description="Fetch one experiment and its measured results by UUID or code (e.g. EXP-0007).",
)
async def get_experiment(
    experiment_id_or_code: Annotated[
        str, Field(min_length=1, max_length=64, description="Experiment UUID or code")
    ],
) -> ExperimentDetail:
    factory = get_async_session_factory()
    async with factory() as session:
        service = ResearchService(session)
        detail = await service.get_experiment_detail(experiment_id_or_code, MCP_SERVICE_PRINCIPAL)
    if detail is None:
        log.info("mcp.tool", tool="get_experiment", found=False)
        return ExperimentDetail(found=False)
    log.info("mcp.tool", tool="get_experiment", found=True, code=detail["code"])
    return ExperimentDetail(found=True, **detail)


@server.tool(
    name="search_literature",
    title="Search literature",
    description=(
        "Semantic search over ingested scientific documents, with optional "
        "research-area and publication-date filters. Returns citable passages."
    ),
)
async def search_literature(
    query: Annotated[str, Field(min_length=2, max_length=500)],
    research_area: Annotated[str | None, Field(max_length=128)] = None,
    date_from: Annotated[dt.date | None, Field(description="Earliest publication date")] = None,
    date_to: Annotated[dt.date | None, Field(description="Latest publication date")] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 8,
) -> LiteratureResult:
    factory = get_async_session_factory()
    async with factory() as session:
        pipeline = RAGPipeline(Retriever(session, LLMEmbeddingProvider(get_llm_client())))
        context = await pipeline.run(
            query,
            MCP_SERVICE_PRINCIPAL,
            filters=RetrievalFilters(
                research_area=research_area, date_from=date_from, date_to=date_to
            ),
            top_k=limit,
        )
    passages = [
        DocumentHit(
            source_id=item.source_id,
            title=item.title,
            snippet=item.snippet[:800],
            relevance=item.relevance_score,
            publication_date=item.publication_date,
            research_area=research_area,
        )
        for item in context.evidence
    ]
    log.info("mcp.tool", tool="search_literature", count=len(passages))
    return LiteratureResult(query=query, count=len(passages), passages=passages)


@server.tool(
    name="get_trial_information",
    title="Get clinical trial",
    description=(
        "Fetch one clinical trial by registry id (e.g. NCT04123456) from the "
        "trials registry, falling back to the local mirror if it is unreachable."
    ),
)
async def get_trial_information(
    registry_id: Annotated[
        str, Field(min_length=3, max_length=64, description="Registry identifier")
    ],
) -> TrialInformation:
    from app.core.errors import ExternalNotFoundError, ExternalServiceError, ToolTimeoutError

    try:
        payload = await get_clinical_client().get_trial(registry_id)
    except ExternalNotFoundError:
        log.info("mcp.tool", tool="get_trial_information", found=False)
        return TrialInformation(found=False, registry_id=registry_id)
    except (ExternalServiceError, ToolTimeoutError) as exc:
        log.warning("mcp.tool_degraded", tool="get_trial_information", error=exc.message)
        factory = get_async_session_factory()
        async with factory() as session:
            mirrored = await ResearchService(session).get_trial(registry_id)
        if mirrored is None:
            return TrialInformation(found=False, registry_id=registry_id, source="unavailable")
        return TrialInformation(found=True, source="database_cache", **mirrored)

    return TrialInformation(
        found=True,
        registry_id=payload.get("registry_id", registry_id),
        title=payload.get("title"),
        condition=payload.get("condition"),
        intervention=payload.get("intervention"),
        phase=payload.get("phase"),
        status=payload.get("status"),
        sponsor=payload.get("sponsor"),
        enrollment=payload.get("enrollment"),
        primary_outcome=payload.get("primary_outcome"),
        summary=payload.get("summary"),
    )


def build_http_app() -> Any:
    """Streamable-HTTP ASGI app with bearer-token auth in front of it."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    class BearerTokenMiddleware(BaseHTTPMiddleware):
        """Minimal, constant-time bearer check.

        A production deployment would verify an OAuth2 access token here (the
        MCP spec's authorisation profile) against the same issuer as the REST
        API. A shared token keeps the demo runnable without standing up an
        identity provider, and the check is in one place either way.
        """

        async def dispatch(self, request: Request, call_next: Any) -> Any:
            if request.url.path.rstrip("/") == "/health":
                return await call_next(request)

            import hmac

            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(token, settings.mcp_auth_token):
                log.warning("mcp.auth_failed", path=request.url.path)
                return JSONResponse(
                    {"error": "unauthorized", "message": "Valid bearer token required"},
                    status_code=401,
                )
            return await call_next(request)

    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.routing import Route

    # DNS-rebinding protection stays ON; we just tell it which Host headers are
    # legitimate for this deployment. Turning it off would be the lazy fix.
    inner = server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=[f"http://{h}" for h in settings.mcp_allowed_hosts],
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        """Propagate the mounted app's lifespan.

        Starlette does not run a sub-application's lifespan when it is
        `Mount`ed, and the streamable-HTTP app starts its session-manager task
        group there. Without this, every request fails with "Task group is not
        initialized".
        """
        async with inner.router.lifespan_context(inner):
            yield

    async def health(_request: Any) -> Any:
        return JSONResponse(
            {"status": "ok", "server": "mcp", "tools": len(await server.list_tools())}
        )

    return Starlette(
        routes=[Route("/health", health), Mount("/", app=inner)],
        middleware=[Middleware(BearerTokenMiddleware)],
        lifespan=lifespan,
    )


def main() -> None:  # pragma: no cover - process entrypoint
    import argparse

    configure_logging(settings.log_level, settings.log_format)
    parser = argparse.ArgumentParser(description="Run the MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="stdio for desktop clients; streamable-http for networked agents",
    )
    parser.add_argument("--host", default=settings.mcp_server_host)
    parser.add_argument("--port", type=int, default=settings.mcp_server_port)
    args = parser.parse_args()

    if args.transport == "stdio":
        log.info("mcp.starting", transport="stdio")
        server.run(transport="stdio")
        return

    import uvicorn

    log.info("mcp.starting", transport="streamable-http", host=args.host, port=args.port)
    uvicorn.run(build_http_app(), host=args.host, port=args.port, log_level="info")
