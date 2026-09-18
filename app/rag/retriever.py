"""pgvector retrieval.

Cosine distance (`<=>`) against an HNSW index. Similarity is reported as
`1 - distance` so higher is better, which is what every caller and the UI
expect.

The single most important line in this file is the access-level filter: it is
applied in SQL, from the authenticated principal, on every query. A document
the caller may not read is never fetched, so it can never reach the prompt, so
no amount of prompt manipulation can extract it.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import Principal
from app.core.config import Settings, get_settings
from app.core.errors import VectorSearchError
from app.core.logging import get_logger
from app.models.document import Document, DocumentChunk
from app.models.enums import AccessLevel, DocumentType, IngestionStatus
from app.rag.embeddings import EmbeddingProvider
from app.schemas.search import RetrievedChunk

log = get_logger(__name__)


@dataclass(slots=True)
class RetrievalFilters:
    """Metadata filters applied alongside the vector search."""

    research_area: str | None = None
    document_types: list[DocumentType] | None = None
    sources: list[str] | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    document_ids: list[uuid.UUID] | None = None
    #: Never set from user input - always derived from the Principal.
    allowed_access_levels: list[AccessLevel] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "research_area": self.research_area,
            "document_types": [d.value for d in self.document_types or []],
            "sources": self.sources,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "access_levels": [a.value for a in self.allowed_access_levels],
        }


def metadata_filter(stmt: Select[Any], filters: RetrievalFilters) -> Select[Any]:
    """Apply metadata predicates to a chunk query (spec-named helper)."""
    conditions = [
        Document.deleted_at.is_(None),
        Document.ingestion_status == IngestionStatus.COMPLETED,
        DocumentChunk.embedding.is_not(None),
    ]

    # Authorisation predicate. Always present; empty list means "public only".
    levels = filters.allowed_access_levels or [AccessLevel.PUBLIC]
    conditions.append(Document.access_level.in_(levels))

    if filters.research_area:
        conditions.append(Document.research_area == filters.research_area)
    if filters.document_types:
        conditions.append(Document.document_type.in_(filters.document_types))
    if filters.sources:
        conditions.append(Document.source.in_(filters.sources))
    if filters.date_from:
        conditions.append(Document.publication_date >= filters.date_from)
    if filters.date_to:
        conditions.append(Document.publication_date <= filters.date_to)
    if filters.document_ids:
        conditions.append(Document.id.in_(filters.document_ids))

    return stmt.where(and_(*conditions))


async def similarity_search(
    session: AsyncSession,
    query_vector: list[float],
    filters: RetrievalFilters,
    *,
    limit: int = 24,
    min_similarity: float = 0.0,
) -> list[RetrievedChunk]:
    """Nearest-neighbour search over document_chunks."""
    distance = DocumentChunk.embedding.cosine_distance(query_vector)
    similarity = (1 - distance).label("similarity")

    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.chunk_index,
            DocumentChunk.document_id,
            DocumentChunk.content,
            DocumentChunk.page_number,
            DocumentChunk.section,
            Document.title,
            Document.publication_date,
            Document.research_area,
            Document.document_type,
            Document.source,
            Document.doi,
            similarity,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .order_by(distance)
        .limit(limit)
    )
    stmt = metadata_filter(stmt, filters)
    if min_similarity > 0:
        stmt = stmt.where(similarity >= min_similarity)

    try:
        rows = (await session.execute(stmt)).all()
    except SQLAlchemyError as exc:
        log.error("rag.similarity_search_failed", error=str(exc))
        raise VectorSearchError("Vector search failed") from exc

    return [
        RetrievedChunk(
            chunk_id=row.id,
            chunk_index=row.chunk_index,
            document_id=row.document_id,
            document_title=row.title,
            content=row.content,
            similarity=float(row.similarity),
            page_number=row.page_number,
            section=row.section,
            publication_date=row.publication_date,
            research_area=row.research_area,
            document_type=row.document_type.value
            if hasattr(row.document_type, "value")
            else row.document_type,
            source=row.source,
            doi=row.doi,
        )
        for row in rows
    ]


class Retriever:
    """Embeds a query and returns the most similar, permitted chunks."""

    def __init__(
        self,
        session: AsyncSession,
        embeddings: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._embeddings = embeddings
        self._settings = settings or get_settings()

    async def retrieve_documents(
        self,
        query: str,
        principal: Principal,
        *,
        filters: RetrievalFilters | None = None,
        limit: int | None = None,
        min_similarity: float | None = None,
    ) -> tuple[list[RetrievedChunk], int]:
        """Retrieve permitted chunks for `query`. Returns (chunks, latency_ms).

        The caller's allowed access levels overwrite whatever was passed in:
        this is the choke point, and it does not take instructions.
        """
        started = time.perf_counter()
        active = filters or RetrievalFilters()
        active.allowed_access_levels = principal.allowed_access_levels

        vector = await self._embeddings.embed_query(query)
        candidates = await similarity_search(
            self._session,
            vector,
            active,
            limit=limit or self._settings.rag_candidate_k,
            min_similarity=(
                self._settings.rag_min_similarity if min_similarity is None else min_similarity
            ),
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        log.info(
            "rag.retrieved",
            query_chars=len(query),
            candidates=len(candidates),
            latency_ms=elapsed,
            filters=active.describe(),
            user_id=principal.user_id,
        )
        return candidates, elapsed

    async def corpus_stats(self) -> dict[str, int]:
        total_docs = await self._session.scalar(
            select(func.count()).select_from(Document).where(Document.deleted_at.is_(None))
        )
        total_chunks = await self._session.scalar(select(func.count()).select_from(DocumentChunk))
        embedded = await self._session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.embedding.is_not(None))
        )
        return {
            "documents": int(total_docs or 0),
            "chunks": int(total_chunks or 0),
            "embedded_chunks": int(embedded or 0),
        }
