"""Retrieval tools (RAG surface for the agent)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, model_validator

from app.auth.permissions import Permission
from app.core.logging import get_logger
from app.llm.factory import get_llm_client
from app.models.enums import DocumentType
from app.rag.embeddings import LLMEmbeddingProvider
from app.rag.pipeline import RAGPipeline
from app.rag.retriever import RetrievalFilters, Retriever
from app.schemas.search import LiteratureSearchRequest
from app.tools.base import Tool, ToolContext, ToolResult

log = get_logger(__name__)


class RetrieveDocumentsRequest(BaseModel):
    """Plain semantic search. The simplest thing that can answer a 'what does
    the literature say' question."""

    query: str = Field(min_length=2, max_length=1000, description="Natural-language search query")
    research_area: str | None = Field(default=None, max_length=128)
    date_from: dt.date | None = Field(default=None, description="Earliest publication date")
    date_to: dt.date | None = Field(default=None, description="Latest publication date")
    limit: int = Field(default=8, ge=1, le=25)

    @model_validator(mode="after")
    def _dates(self) -> RetrieveDocumentsRequest:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


def _build_pipeline(session) -> RAGPipeline:  # type: ignore[no-untyped-def]
    embeddings = LLMEmbeddingProvider(get_llm_client())
    return RAGPipeline(Retriever(session, embeddings))


class RetrieveDocumentsTool(Tool):
    name = "retrieve_documents"
    description = (
        "Semantic search over the ingested scientific document corpus. Returns "
        "passages with citable source ids. Use for questions about published "
        "findings, evidence or prior work."
    )
    input_model = RetrieveDocumentsRequest
    required_permission = Permission.DOCUMENTS_READ
    #: NOT cacheable: results are filtered by the caller's access level, so a
    #: shared cache entry could leak restricted passages to a junior researcher.
    cacheable = False
    timeout_seconds = 25.0

    async def run(self, args: RetrieveDocumentsRequest, ctx: ToolContext) -> ToolResult:
        async with ctx.session_factory() as session:
            pipeline = _build_pipeline(session)
            context = await pipeline.run(
                args.query,
                ctx.principal,
                filters=RetrievalFilters(
                    research_area=args.research_area,
                    date_from=args.date_from,
                    date_to=args.date_to,
                ),
                top_k=args.limit,
            )

        if context.is_empty:
            return ToolResult(
                tool=self.name,
                ok=True,
                summary="No matching passages in the document corpus.",
                data=[],
                evidence=[],
                count=0,
                latency_ms=context.retrieval_latency_ms,
            )

        return ToolResult(
            tool=self.name,
            ok=True,
            summary=f"Retrieved {len(context.evidence)} passage(s) from the corpus.",
            data=[
                {
                    "source_id": item.source_id,
                    "title": item.title,
                    "snippet": item.snippet[:600],
                    "relevance": item.relevance_score,
                    "publication_date": item.publication_date.isoformat()
                    if item.publication_date
                    else None,
                }
                for item in context.evidence
            ],
            evidence=context.evidence,
            count=len(context.evidence),
            latency_ms=context.retrieval_latency_ms,
        )


class SearchLiteratureTool(Tool):
    name = "search_literature"
    description = (
        "Filtered literature search over the document corpus. Same index as "
        "retrieve_documents but with explicit metadata filters: publication "
        "date range, research area, document type and source. Prefer this when "
        "the user specified any of those."
    )
    input_model = LiteratureSearchRequest
    required_permission = Permission.SEARCH_LITERATURE
    cacheable = False  # access-level filtered; see RetrieveDocumentsTool
    timeout_seconds = 25.0

    async def run(self, args: LiteratureSearchRequest, ctx: ToolContext) -> ToolResult:
        async with ctx.session_factory() as session:
            pipeline = _build_pipeline(session)
            context = await pipeline.run(
                args.query,
                ctx.principal,
                filters=RetrievalFilters(
                    research_area=args.research_area,
                    document_types=list(args.document_types) if args.document_types else None,
                    sources=args.sources,
                    date_from=args.date_from,
                    date_to=args.date_to,
                ),
                top_k=args.limit,
            )

        applied = {k: v for k, v in context.filters.items() if v}
        return ToolResult(
            tool=self.name,
            ok=True,
            summary=(
                f"{len(context.evidence)} passage(s) matched "
                f"(filters: {applied or 'none beyond access control'})."
            ),
            data=[
                {
                    "source_id": item.source_id,
                    "title": item.title,
                    "snippet": item.snippet[:600],
                    "relevance": item.relevance_score,
                    "publication_date": item.publication_date.isoformat()
                    if item.publication_date
                    else None,
                }
                for item in context.evidence
            ],
            evidence=context.evidence,
            count=len(context.evidence),
            latency_ms=context.retrieval_latency_ms,
        )


DOCUMENT_TYPE_VALUES = [t.value for t in DocumentType]
