"""Document ingestion: upload, extraction, chunking, embedding."""

from __future__ import annotations

import io

import pytest

from app.models.enums import IngestionStatus, RoleName
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def build_pdf(title: str, body: str) -> bytes:
    """Render a real PDF in memory so extraction is genuinely exercised."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(buffer, pagesize=A4, title=title)
    document.build(
        [
            Paragraph(title, styles["Title"]),
            Paragraph("Abstract", styles["Heading2"]),
            Paragraph(body, styles["BodyText"]),
            Paragraph("Results", styles["Heading2"]),
            Paragraph(body, styles["BodyText"]),
        ]
    )
    return buffer.getvalue()


class TestUploadValidation:
    async def test_non_pdf_bytes_rejected_despite_a_pdf_content_type(
        self, api_client, auth_headers
    ):
        """Content-Type is attacker-controlled; the magic bytes are checked."""
        response = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("evil.pdf", b"#!/bin/sh\nrm -rf /", "application/pdf")},
        )
        assert response.status_code == 422
        assert "magic bytes" in response.json()["error"]["message"]

    async def test_empty_file_rejected(self, api_client, auth_headers):
        response = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        assert response.status_code == 422

    async def test_upload_requires_authentication(self, api_client):
        response = await api_client.post(
            "/api/v1/documents",
            files={"file": ("t.pdf", b"%PDF-1.4 x", "application/pdf")},
        )
        assert response.status_code == 401


class TestIngestionPipeline:
    async def test_full_pipeline_produces_searchable_chunks(
        self, api_client, auth_headers, seed_roles_and_users, tmp_path, monkeypatch
    ):
        """Runs the real pipeline synchronously (no Celery broker needed)."""
        from app.database.session import sync_session_scope
        from app.documents.ingestion import DocumentIngestionPipeline
        from app.documents.storage import LocalObjectStorage, set_storage

        set_storage(LocalObjectStorage(tmp_path))

        pdf = build_pdf(
            "CDK4/6 inhibition in luminal breast cancer",
            "Rb pathway integrity predicted durable benefit from CDK4/6 inhibition. "
            "The hazard ratio was 0.58 with a p-value of 0.0003 across 214 participants. " * 6,
        )
        response = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("cdk.pdf", pdf, "application/pdf")},
            data={"title": "CDK4/6 inhibition study", "research_area": "oncology"},
        )
        assert response.status_code == 202
        document_id = response.json()["document_id"]

        # The HTTP request only stored bytes.
        assert response.json()["status"] == IngestionStatus.UPLOADED.value

        import uuid

        with sync_session_scope() as session:
            outcome = DocumentIngestionPipeline(session).process(uuid.UUID(document_id))

        assert outcome.status == IngestionStatus.COMPLETED
        assert outcome.page_count >= 1
        assert outcome.chunk_count >= 1
        assert outcome.embedded == outcome.chunk_count

        detail = (
            await api_client.get(
                f"/api/v1/documents/{document_id}",
                headers=auth_headers[RoleName.RESEARCHER],
            )
        ).json()
        assert detail["ingestion_status"] == "completed"
        assert detail["chunks"]
        assert "Rb pathway" in " ".join(c["content"] for c in detail["chunks"])

        found = (
            await api_client.post(
                "/api/v1/search/literature",
                headers=auth_headers[RoleName.RESEARCHER],
                json={"query": "Rb pathway integrity CDK4/6 inhibition", "limit": 5},
            )
        ).json()
        assert any("CDK4/6" in r["document_title"] for r in found["results"])
        set_storage(None)

    async def test_reingestion_is_idempotent(
        self, api_client, auth_headers, seed_roles_and_users, tmp_path
    ):
        """Celery is at-least-once; running twice must not duplicate chunks."""
        import uuid

        from sqlalchemy import func, select

        from app.database.session import sync_session_scope
        from app.documents.ingestion import DocumentIngestionPipeline
        from app.documents.storage import LocalObjectStorage, set_storage
        from app.models.document import DocumentChunk

        set_storage(LocalObjectStorage(tmp_path))
        pdf = build_pdf("Idempotency test", "Content about biomarkers in a study. " * 40)
        response = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("idem.pdf", pdf, "application/pdf")},
        )
        document_id = uuid.UUID(response.json()["document_id"])

        counts = []
        for _ in range(2):
            with sync_session_scope() as session:
                DocumentIngestionPipeline(session).process(document_id)
                counts.append(
                    session.scalar(
                        select(func.count())
                        .select_from(DocumentChunk)
                        .where(DocumentChunk.document_id == document_id)
                    )
                )
        assert counts[0] == counts[1]
        set_storage(None)

    async def test_duplicate_content_is_not_reingested(
        self, api_client, auth_headers, seed_roles_and_users, tmp_path
    ):
        from app.documents.storage import LocalObjectStorage, set_storage

        set_storage(LocalObjectStorage(tmp_path))
        pdf = build_pdf("Dedup test", "Identical bytes both times. " * 30)
        first = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("a.pdf", pdf, "application/pdf")},
        )
        second = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("b.pdf", pdf, "application/pdf")},
        )
        assert second.json()["duplicate_of"] == first.json()["document_id"]
        set_storage(None)

    async def test_corrupt_pdf_marks_the_document_failed_without_raising_to_the_user(
        self, api_client, auth_headers, seed_roles_and_users, tmp_path
    ):
        import uuid

        from app.core.errors import ValidationError
        from app.database.session import sync_session_scope
        from app.documents.storage import LocalObjectStorage, set_storage
        from app.models.document import Document

        storage = LocalObjectStorage(tmp_path)
        set_storage(storage)

        # A file with valid magic bytes but no parseable content.
        response = await api_client.post(
            "/api/v1/documents",
            headers=auth_headers[RoleName.RESEARCHER],
            files={"file": ("broken.pdf", b"%PDF-1.4\nnot really a pdf body", "application/pdf")},
        )
        assert response.status_code == 202
        document_id = uuid.UUID(response.json()["document_id"])

        from app.documents.ingestion import DocumentIngestionPipeline

        with sync_session_scope() as session:
            with pytest.raises((ValidationError, Exception)):
                DocumentIngestionPipeline(session).process(document_id)

        with sync_session_scope() as session:
            document = session.get(Document, document_id)
            assert document.ingestion_status == IngestionStatus.FAILED
            assert document.ingestion_error
        set_storage(None)


class TestStorageSafety:
    def test_path_traversal_is_refused(self, tmp_path):
        from app.documents.storage import LocalObjectStorage, StorageError

        storage = LocalObjectStorage(tmp_path)
        with pytest.raises(StorageError):
            storage.put("../../etc/passwd", b"x")

    def test_key_is_content_addressed(self):
        from app.documents.storage import build_key, sha256_of

        data = b"%PDF-1.4 content"
        assert sha256_of(data) in build_key(sha256_of(data), "a.pdf")

    def test_filename_is_sanitised(self):
        from app.documents.storage import safe_filename

        assert "/" not in safe_filename("../../etc/passwd")
        assert safe_filename("report (final).pdf").endswith("final_.pdf")
