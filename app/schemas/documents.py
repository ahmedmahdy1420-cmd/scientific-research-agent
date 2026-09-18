"""Document upload / listing schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.models.enums import AccessLevel, DocumentType, IngestionStatus
from app.schemas.common import ORMModel


class DocumentUploadResponse(BaseModel):
    """202: the HTTP request only stores bytes; parsing happens in Celery."""

    document_id: uuid.UUID
    status: IngestionStatus
    task_id: str | None = None
    message: str
    duplicate_of: uuid.UUID | None = Field(
        default=None, description="Set when identical bytes were already ingested"
    )


class DocumentResponse(ORMModel):
    id: uuid.UUID
    title: str
    filename: str
    document_type: DocumentType
    access_level: AccessLevel
    authors: list[str]
    source: str | None
    doi: str | None
    journal: str | None
    publication_date: dt.date | None
    research_area: str | None
    abstract: str | None
    keywords: list[str]
    ingestion_status: IngestionStatus
    ingestion_error: str | None
    page_count: int
    chunk_count: int
    size_bytes: int
    owner_id: uuid.UUID | None
    created_at: dt.datetime
    updated_at: dt.datetime


class DocumentChunkResponse(ORMModel):
    id: uuid.UUID
    chunk_index: int
    content: str
    page_number: int | None
    section: str | None
    token_count: int


class DocumentDetailResponse(DocumentResponse):
    chunks: list[DocumentChunkResponse] = Field(default_factory=list)


class DocumentListQuery(BaseModel):
    research_area: str | None = None
    document_type: DocumentType | None = None
    status: IngestionStatus | None = None
    query: str | None = Field(default=None, max_length=255)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class IngestionStatusResponse(BaseModel):
    document_id: uuid.UUID
    status: IngestionStatus
    attempts: int
    error: str | None = None
    chunk_count: int = 0
    page_count: int = 0
    updated_at: dt.datetime | None = None
