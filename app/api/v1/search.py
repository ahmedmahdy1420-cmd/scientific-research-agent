"""Direct search endpoints.

These expose the same capabilities the agent uses as tools, without the agent.
Useful for a UI that wants a plain search box, and useful for debugging: if an
answer looks wrong, hitting `/search/literature` with the same query shows
whether the problem is retrieval or synthesis.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from app.api.deps import CacheDep, RateLimitedPrincipal
from app.auth.dependencies import DbSession, require_permissions
from app.auth.permissions import Permission
from app.core.logging import get_logger
from app.core.redis import CacheKey
from app.llm.factory import get_llm_client
from app.rag.embeddings import LLMEmbeddingProvider
from app.rag.pipeline import RAGPipeline
from app.rag.retriever import RetrievalFilters, Retriever
from app.schemas.common import ErrorResponse
from app.schemas.search import (
    ClinicalTrialResult,
    ClinicalTrialSearchRequest,
    ClinicalTrialSearchResponse,
    LiteratureSearchRequest,
    LiteratureSearchResponse,
)

router = APIRouter(prefix="/search", tags=["search"])
log = get_logger(__name__)


@router.post(
    "/literature",
    response_model=LiteratureSearchResponse,
    summary="Semantic search over the document corpus",
    description=(
        "pgvector cosine similarity with metadata filters, followed by optional "
        "reranking. Results are always filtered to the caller's access level."
    ),
    dependencies=[Depends(require_permissions(Permission.SEARCH_LITERATURE))],
    responses={403: {"model": ErrorResponse}},
)
async def search_literature(
    payload: LiteratureSearchRequest,
    principal: RateLimitedPrincipal,
    session: DbSession,
) -> LiteratureSearchResponse:
    started = time.perf_counter()
    pipeline = RAGPipeline(Retriever(session, LLMEmbeddingProvider(get_llm_client())))
    context = await pipeline.run(
        payload.query,
        principal,
        filters=RetrievalFilters(
            research_area=payload.research_area,
            document_types=list(payload.document_types) if payload.document_types else None,
            sources=payload.sources,
            date_from=payload.date_from,
            date_to=payload.date_to,
        ),
        top_k=payload.limit,
    )
    return LiteratureSearchResponse(
        query=payload.query,
        results=context.chunks,
        total=len(context.chunks),
        latency_ms=int((time.perf_counter() - started) * 1000),
        reranked=context.reranked,
        filters_applied=context.filters,
    )


@router.post(
    "/clinical-trials",
    response_model=ClinicalTrialSearchResponse,
    summary="Search the clinical-trials registry",
    description=(
        "Calls the external registry with retries and exponential backoff. If "
        "the registry is unavailable, falls back to the local mirror and sets "
        "`degraded=true` rather than failing the request."
    ),
    dependencies=[Depends(require_permissions(Permission.SEARCH_TRIALS))],
    responses={403: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
async def search_clinical_trials(
    payload: ClinicalTrialSearchRequest,
    principal: RateLimitedPrincipal,
    session: DbSession,
    cache: CacheDep,
) -> ClinicalTrialSearchResponse:
    started = time.perf_counter()

    # Registry data is public and identical for every caller, so a shared cache
    # entry is safe here (unlike document search, which is access-filtered).
    key = CacheKey.build("search:trials", payload.model_dump(mode="json"))
    cached = await cache.get(key)
    if cached:
        return ClinicalTrialSearchResponse(
            condition=payload.condition,
            results=[ClinicalTrialResult(**r) for r in cached["results"]],
            total=cached["total"],
            source="database_cache",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    from app.database.session import get_async_session_factory
    from app.tools.base import ToolContext
    from app.tools.clinical_trials import SearchClinicalTrialsTool

    # Reuse the agent's tool so the REST path and the agent path share one
    # implementation of retries, timeouts and the stale-mirror fallback.
    tool = SearchClinicalTrialsTool()
    ctx = ToolContext(principal=principal, session_factory=get_async_session_factory())
    result = await tool.run(payload, ctx)

    if not result.ok:
        from app.core.errors import ExternalServiceError

        raise ExternalServiceError(result.error_message or "Trial search failed")

    results = [ClinicalTrialResult(**r) for r in (result.data or [])]
    if not result.degraded:
        await cache.set(
            key,
            {"results": [r.model_dump(mode="json") for r in results], "total": len(results)},
            ttl=600,
        )

    return ClinicalTrialSearchResponse(
        condition=payload.condition,
        results=results,
        total=len(results),
        source="stale_cache" if result.degraded else "external_api",
        latency_ms=int((time.perf_counter() - started) * 1000),
        degraded=result.degraded,
        degraded_reason=result.summary if result.degraded else None,
    )
