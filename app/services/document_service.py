"""Document upload and listing.

The HTTP request does four cheap things — validate, hash, store, insert a row —
and then hands off to Celery. It returns 202 with a document id the client can
poll. Anything that touches an LLM or parses a PDF happens in the worker.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import Permission, Principal
from app.core.config import Settings, get_settings
from app.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    ResourceAccessDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.documents.extraction import validate_pdf_bytes
from app.documents.storage import ObjectStorage, build_key, get_storage, sha256_of
from app.models.document import Document, DocumentChunk
from app.models.enums import AccessLevel, DocumentType, IngestionStatus
from app.schemas.documents import DocumentListQuery

log = get_logger(__name__)


class DocumentService:
    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._storage = storage or get_storage(self._settings)

    # ---------------------------------------------------------------- upload
    async def upload(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str | None,
        principal: Principal,
        title: str | None = None,
        document_type: DocumentType = DocumentType.PAPER,
        access_level: AccessLevel = AccessLevel.INTERNAL,
        research_area: str | None = None,
        source: str | None = None,
    ) -> tuple[Document, bool]:
        """Store an uploaded PDF. Returns (document, is_duplicate)."""
        max_bytes = self._settings.max_upload_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise PayloadTooLargeError(
                f"File exceeds the {self._settings.max_upload_mb}MB limit",
                details={"size_bytes": len(data)},
            )
        if content_type and content_type not in self._settings.allowed_upload_types:
            raise ValidationError(
                f"Unsupported content type {content_type!r}",
                details={"allowed": self._settings.allowed_upload_types},
            )
        # Trust the bytes, not the header.
        validate_pdf_bytes(data, max_bytes)

        # A user cannot upload a document more sensitive than they can read.
        if not principal.can_read_access_level(access_level):
            raise ResourceAccessDeniedError(
                f"You cannot create a document at access level {access_level}",
                details={"max_access_level": principal.max_access_level},
            )

        content_hash = sha256_of(data)
        existing = (
            (
                await self._session.execute(
                    select(Document).where(Document.content_sha256 == content_hash)
                )
            )
            .unique()
            .scalar_one_or_none()
        )
        if existing is not None:
            log.info(
                "documents.duplicate_upload",
                document_id=str(existing.id),
                user_id=principal.user_id,
            )
            return existing, True

        key = build_key(content_hash, filename)
        self._storage.put(key, data, content_type or "application/pdf")

        document = Document(
            title=(title or f"Untitled ({filename})")[:512],
            filename=filename[:512],
            storage_key=key,
            content_sha256=content_hash,
            size_bytes=len(data),
            document_type=document_type,
            access_level=access_level,
            research_area=research_area,
            source=source,
            owner_id=uuid.UUID(principal.user_id),
            ingestion_status=IngestionStatus.UPLOADED,
        )
        self._session.add(document)
        await self._session.flush()

        log.info(
            "documents.uploaded",
            document_id=str(document.id),
            bytes=len(data),
            user_id=principal.user_id,
            access_level=access_level.value,
        )
        return document, False

    # ----------------------------------------------------------------- reads
    def _visibility_clause(self, principal: Principal) -> Any:
        return Document.access_level.in_(principal.allowed_access_levels)

    async def list_documents(
        self, query: DocumentListQuery, principal: Principal
    ) -> tuple[list[Document], int]:
        base = select(Document).where(
            Document.deleted_at.is_(None), self._visibility_clause(principal)
        )
        counter = (
            select(func.count())
            .select_from(Document)
            .where(Document.deleted_at.is_(None), self._visibility_clause(principal))
        )

        if query.research_area:
            base = base.where(Document.research_area == query.research_area)
            counter = counter.where(Document.research_area == query.research_area)
        if query.document_type:
            base = base.where(Document.document_type == query.document_type)
            counter = counter.where(Document.document_type == query.document_type)
        if query.status:
            base = base.where(Document.ingestion_status == query.status)
            counter = counter.where(Document.ingestion_status == query.status)
        if query.query:
            term = f"%{query.query.strip()}%"
            predicate = or_(
                Document.title.ilike(term),
                Document.abstract.ilike(term),
                Document.doi.ilike(term),
            )
            base = base.where(predicate)
            counter = counter.where(predicate)

        total = int((await self._session.execute(counter)).scalar() or 0)
        rows = (
            (
                await self._session.execute(
                    base.order_by(desc(Document.created_at)).limit(query.limit).offset(query.offset)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        return list(rows), total

    async def get_document(self, document_id: uuid.UUID, principal: Principal) -> Document:
        document = (
            (
                await self._session.execute(
                    select(Document).where(
                        Document.id == document_id, Document.deleted_at.is_(None)
                    )
                )
            )
            .unique()
            .scalar_one_or_none()
        )
        if document is None:
            raise NotFoundError(f"No document with id {document_id}")
        if not principal.can_read_access_level(document.access_level):
            log.warning(
                "documents.access_denied",
                document_id=str(document_id),
                user_id=principal.user_id,
                required=document.access_level.value,
            )
            raise ResourceAccessDeniedError("You do not have access to this document")
        return document

    async def get_chunks(self, document_id: uuid.UUID, *, limit: int = 20) -> list[DocumentChunk]:
        rows = (
            (
                await self._session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index)
                    .limit(limit)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        return list(rows)

    async def soft_delete(self, document_id: uuid.UUID, principal: Principal) -> Document:
        if not principal.has_permission(Permission.DOCUMENTS_DELETE):
            raise ResourceAccessDeniedError(
                "You are not permitted to delete documents",
                details={"required": Permission.DOCUMENTS_DELETE},
            )
        document = await self.get_document(document_id, principal)
        from app.documents.ingestion import utcnow

        document.deleted_at = utcnow()
        await self._session.flush()
        log.warning(
            "documents.soft_deleted",
            document_id=str(document_id),
            user_id=principal.user_id,
        )
        return document
