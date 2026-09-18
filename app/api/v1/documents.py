"""Document upload and browsing."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.api.deps import DocumentServiceDep, RateLimitedPrincipal
from app.auth.dependencies import CurrentPrincipal, RequireDocumentUpload
from app.core.errors import PayloadTooLargeError
from app.core.logging import get_logger
from app.models.enums import AccessLevel, DocumentType, IngestionStatus
from app.schemas.common import ErrorResponse, Page
from app.schemas.documents import (
    DocumentChunkResponse,
    DocumentDetailResponse,
    DocumentListQuery,
    DocumentResponse,
    DocumentUploadResponse,
    IngestionStatusResponse,
)

router = APIRouter(prefix="/documents", tags=["documents"])
log = get_logger(__name__)

#: Read the upload in bounded chunks so a lying Content-Length cannot be used
#: to stream an unbounded body into memory.
READ_CHUNK = 1024 * 1024


@router.post(
    "",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a PDF for ingestion",
    description=(
        "Stores the file and queues a Celery ingestion job (extract, chunk, "
        "embed). Returns 202 immediately with a document id to poll. Uploading "
        "bytes that were already ingested returns the existing document rather "
        "than duplicating it."
    ),
    responses={413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def upload_document(
    principal: RequireDocumentUpload,
    service: DocumentServiceDep,
    file: Annotated[UploadFile, File(description="PDF file")],
    title: Annotated[str | None, Form(max_length=512)] = None,
    document_type: Annotated[DocumentType, Form()] = DocumentType.PAPER,
    access_level: Annotated[AccessLevel, Form()] = AccessLevel.INTERNAL,
    research_area: Annotated[str | None, Form(max_length=128)] = None,
    source: Annotated[str | None, Form(max_length=255)] = None,
) -> DocumentUploadResponse:
    from app.core.config import get_settings

    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    buffer = bytearray()
    while chunk := await file.read(READ_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise PayloadTooLargeError(f"File exceeds the {get_settings().max_upload_mb}MB limit")

    document, duplicate = await service.upload(
        data=bytes(buffer),
        filename=file.filename or "upload.pdf",
        content_type=file.content_type,
        principal=principal,
        title=title,
        document_type=document_type,
        access_level=access_level,
        research_area=research_area,
        source=source,
    )

    if duplicate:
        return DocumentUploadResponse(
            document_id=document.id,
            status=document.ingestion_status,
            message="Identical content was already ingested; returning the existing document.",
            duplicate_of=document.id,
        )

    # Queue the heavy work. If the broker is unreachable the document stays in
    # `uploaded` and can be re-queued; the upload itself is not lost.
    task_id: str | None = None
    try:
        from app.workers.tasks import ingest_document

        task = ingest_document.delay(str(document.id))
        task_id = task.id
        document.ingestion_task_id = task_id
    except Exception as exc:  # noqa: BLE001 - broker down must not lose the upload
        log.error("documents.enqueue_failed", document_id=str(document.id), error=str(exc))

    return DocumentUploadResponse(
        document_id=document.id,
        status=document.ingestion_status,
        task_id=task_id,
        message=(
            "Upload accepted. Ingestion is running in the background; poll "
            f"GET /documents/{document.id} for status."
            if task_id
            else "Upload stored, but the ingestion queue is unavailable. It will "
            "need to be re-queued."
        ),
    )


@router.get(
    "",
    response_model=Page[DocumentResponse],
    summary="List documents the caller may see",
    description=(
        "Results are filtered by the caller's access level: a researcher never "
        "sees restricted documents, and the filter is applied in SQL."
    ),
)
async def list_documents(
    principal: RateLimitedPrincipal,
    service: DocumentServiceDep,
    research_area: str | None = Query(default=None, max_length=128),
    document_type: DocumentType | None = Query(default=None),
    ingestion_status: IngestionStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=255, description="Title/abstract/DOI search"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[DocumentResponse]:
    query = DocumentListQuery(
        research_area=research_area,
        document_type=document_type,
        status=ingestion_status,
        query=q,
        limit=limit,
        offset=offset,
    )
    rows, total = await service.list_documents(query, principal)
    return Page[DocumentResponse](
        items=[DocumentResponse.model_validate(d) for d in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{document_id}",
    response_model=DocumentDetailResponse,
    summary="One document with a sample of its chunks",
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_document(
    document_id: uuid.UUID,
    principal: CurrentPrincipal,
    service: DocumentServiceDep,
    chunk_limit: int = Query(default=10, ge=0, le=100),
) -> DocumentDetailResponse:
    document = await service.get_document(document_id, principal)
    chunks = await service.get_chunks(document_id, limit=chunk_limit) if chunk_limit else []
    payload = DocumentDetailResponse.model_validate(document)
    payload.chunks = [DocumentChunkResponse.model_validate(c) for c in chunks]
    return payload


@router.get(
    "/{document_id}/status",
    response_model=IngestionStatusResponse,
    summary="Ingestion status (poll this after upload)",
    responses={404: {"model": ErrorResponse}},
)
async def ingestion_status(
    document_id: uuid.UUID,
    principal: CurrentPrincipal,
    service: DocumentServiceDep,
) -> IngestionStatusResponse:
    document = await service.get_document(document_id, principal)
    return IngestionStatusResponse(
        document_id=document.id,
        status=document.ingestion_status,
        attempts=document.ingestion_attempts,
        error=document.ingestion_error,
        chunk_count=document.chunk_count,
        page_count=document.page_count,
        updated_at=document.updated_at,
    )


@router.post(
    "/{document_id}/reingest",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run ingestion for a document",
    description="Idempotent: existing chunks are replaced, not duplicated.",
)
async def reingest(
    document_id: uuid.UUID,
    principal: RequireDocumentUpload,
    service: DocumentServiceDep,
) -> DocumentUploadResponse:
    document = await service.get_document(document_id, principal)
    from app.workers.tasks import ingest_document

    task = ingest_document.delay(str(document.id))
    return DocumentUploadResponse(
        document_id=document.id,
        status=document.ingestion_status,
        task_id=task.id,
        message="Re-ingestion queued.",
    )
