"""Document ingestion pipeline.

    upload  ->  object storage  ->  extract  ->  clean  ->  metadata
            ->  chunk  ->  embed  ->  postgres + pgvector

Only the first step happens inside the HTTP request. Everything after it runs in
Celery, because extraction and embedding are seconds-to-minutes of CPU and
network: doing them inline would hold a worker, blow past any sane gateway
timeout, and lose all progress if the client disconnects.

The state machine on `documents.ingestion_status` is what makes it restartable:

    uploaded -> processing -> extracted -> chunked -> embedded -> completed
                                  |
                                  +-------------------------------> failed

Idempotency: the pipeline is keyed on the content hash, and re-running it for a
document deletes that document's chunks before re-inserting. A retried task
therefore converges on the same result instead of duplicating chunks — which
matters, because Celery retries are at-least-once, not exactly-once.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import EmbeddingError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.documents.extraction import extract_pdf
from app.documents.storage import ObjectStorage, get_storage
from app.models.document import Document, DocumentChunk
from app.models.enums import IngestionStatus
from app.rag.chunking import Chunk, chunk_pages

log = get_logger(__name__)


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from synchronous code, whether or not a loop is running.

    The pipeline is synchronous (it runs inside a Celery worker) but the
    embedding provider is async. `asyncio.run` alone is not enough: it raises
    when a loop is already running, which happens whenever the pipeline is
    driven from async code - an async test today, an in-process async caller
    tomorrow. Falling back to a worker thread with its own loop keeps a single
    code path correct in both cases.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@dataclass(slots=True)
class IngestionOutcome:
    document_id: uuid.UUID
    status: IngestionStatus
    page_count: int
    chunk_count: int
    embedded: int
    error: str | None = None


class DocumentIngestionPipeline:
    """Synchronous pipeline, driven by a Celery task.

    Synchronous on purpose: a Celery worker process has no event loop to share,
    the work is CPU-bound (PDF parsing) plus one batched network call, and the
    sync SQLAlchemy session keeps the transaction boundaries obvious.
    """

    def __init__(
        self,
        session: Session,
        storage: ObjectStorage | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._storage = storage or get_storage(self._settings)

    # ------------------------------------------------------------------ main
    def process(self, document_id: uuid.UUID) -> IngestionOutcome:
        document = self._session.get(Document, document_id)
        if document is None:
            raise NotFoundError(f"No document with id {document_id}")

        log.info(
            "ingestion.started",
            document_id=str(document_id),
            attempt=document.ingestion_attempts + 1,
        )
        document.ingestion_status = IngestionStatus.PROCESSING
        document.ingestion_attempts += 1
        document.ingestion_error = None
        self._session.commit()

        try:
            data = self._storage.get(document.storage_key)
            extracted = extract_pdf(data)

            document.page_count = extracted.page_count
            self._apply_metadata(document, extracted)
            document.ingestion_status = IngestionStatus.EXTRACTED
            self._session.commit()

            chunks = chunk_pages(extracted.pages, self._settings)
            if not chunks:
                raise ValidationError("Document produced no usable chunks after cleaning")

            self._replace_chunks(document, chunks)
            document.ingestion_status = IngestionStatus.CHUNKED
            document.chunk_count = len(chunks)
            self._session.commit()

            embedded = self._embed_chunks(document)
            document.ingestion_status = IngestionStatus.COMPLETED
            self._session.commit()

            log.info(
                "ingestion.completed",
                document_id=str(document_id),
                pages=extracted.page_count,
                chunks=len(chunks),
                embedded=embedded,
            )
            return IngestionOutcome(
                document_id=document_id,
                status=IngestionStatus.COMPLETED,
                page_count=extracted.page_count,
                chunk_count=len(chunks),
                embedded=embedded,
            )
        except Exception as exc:
            self._session.rollback()
            document = self._session.get(Document, document_id)
            if document is not None:
                document.ingestion_status = IngestionStatus.FAILED
                document.ingestion_error = str(exc)[:2000]
                self._session.commit()
            log.error(
                "ingestion.failed",
                document_id=str(document_id),
                error=str(exc)[:500],
                error_type=type(exc).__name__,
            )
            raise

    # ------------------------------------------------------------- internals
    @staticmethod
    def _apply_metadata(document: Document, extracted) -> None:  # type: ignore[no-untyped-def]
        """Fill only what the uploader did not already supply.

        Uploader-provided metadata always wins: a human who typed the research
        area knows better than a regex over page one.
        """
        if extracted.title and document.title.startswith("Untitled"):
            document.title = extracted.title[:512]
        if extracted.authors and not document.authors:
            document.authors = extracted.authors
        if extracted.doi and not document.doi:
            document.doi = extracted.doi[:255]
        if extracted.publication_date and not document.publication_date:
            document.publication_date = extracted.publication_date
        if extracted.keywords and not document.keywords:
            document.keywords = extracted.keywords
        if extracted.abstract and not document.abstract:
            document.abstract = extracted.abstract

    def _replace_chunks(self, document: Document, chunks: list[Chunk]) -> None:
        """Delete-then-insert makes a retry idempotent."""
        self._session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
        metadata_common = {
            "document_type": document.document_type.value,
            "access_level": document.access_level.value,
            "research_area": document.research_area,
            "source": document.source,
            "publication_date": (
                document.publication_date.isoformat() if document.publication_date else None
            ),
        }
        for chunk in chunks:
            self._session.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    page_number=chunk.page_number,
                    section=chunk.section,
                    chunk_metadata={**metadata_common, "section": chunk.section},
                )
            )

    def _embed_chunks(self, document: Document) -> int:
        """Embed every chunk of this document in batches."""
        from app.llm.factory import get_llm_client
        from app.rag.embeddings import LLMEmbeddingProvider

        rows = list(
            self._session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document.id)
                .order_by(DocumentChunk.chunk_index)
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0

        provider = LLMEmbeddingProvider(get_llm_client(), self._settings)
        texts = [self._embedding_text(document, row) for row in rows]

        try:
            vectors = run_sync(provider.embed_texts(texts))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"Embedding failed: {exc}") from exc

        for row, vector in zip(rows, vectors, strict=True):
            row.embedding = vector
            row.embedding_model = provider.model

        document.ingestion_status = IngestionStatus.EMBEDDED
        self._session.commit()
        return len(rows)

    @staticmethod
    def _embedding_text(document: Document, chunk: DocumentChunk) -> str:
        """Prepend document context to each chunk before embedding.

        A chunk from the middle of a paper often says "the treatment group
        improved" with no indication of which treatment or which disease.
        Embedding the title and section alongside it makes the vector reflect
        what the passage is actually about, which measurably improves recall
        on short queries.
        """
        header = document.title
        if chunk.section:
            header = f"{header} - {chunk.section}"
        if document.research_area:
            header = f"{header} ({document.research_area})"
        return f"{header}\n\n{chunk.content}"


def compute_ingestion_stats(session: Session) -> dict[str, int]:
    """Corpus health, surfaced on the readiness endpoint and the UI."""
    rows = session.execute(
        select(Document.ingestion_status, func.count())
        .where(Document.deleted_at.is_(None))
        .group_by(Document.ingestion_status)
    ).all()
    stats = {status.value: 0 for status in IngestionStatus}
    for status, count in rows:
        stats[status.value if hasattr(status, "value") else str(status)] = int(count)
    return stats


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
