"""Celery tasks.

Retry policy: `autoretry_for` transient classes with exponential backoff and
jitter, capped at `max_retries`. Non-transient failures (a corrupt PDF, a
validation error) are *not* retried — retrying a malformed file three times
just delays the failure and triples the log noise.

Every task is idempotent, which is what makes `acks_late` safe: the same task
delivered twice converges on the same database state.
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.core.errors import EmbeddingError, ExternalServiceError, ValidationError
from app.core.logging import LogContext, get_logger
from app.database.session import sync_session_scope
from app.documents.ingestion import DocumentIngestionPipeline
from app.models.document import Document
from app.models.enums import IngestionStatus
from app.workers.celery_app import celery_app

log = get_logger(__name__)

#: Only these are worth retrying. A ValidationError never is.
TRANSIENT = (EmbeddingError, ExternalServiceError, ConnectionError, TimeoutError, OSError)


@celery_app.task(
    bind=True,
    name="documents.ingest",
    autoretry_for=TRANSIENT,
    retry_backoff=True,  # 1s, 2s, 4s ...
    retry_backoff_max=300,
    retry_jitter=True,  # avoids a synchronised retry stampede
    max_retries=4,
    acks_late=True,
)
def ingest_document(self: Task, document_id: str) -> dict[str, Any]:
    """Run the full ingestion pipeline for one document."""
    with LogContext(document_id=document_id, task_id=self.request.id):
        log.info("task.ingest_started", retries=self.request.retries)
        try:
            with sync_session_scope() as session:
                pipeline = DocumentIngestionPipeline(session)
                outcome = pipeline.process(uuid.UUID(document_id))
            return {
                "document_id": str(outcome.document_id),
                "status": outcome.status.value,
                "pages": outcome.page_count,
                "chunks": outcome.chunk_count,
                "embedded": outcome.embedded,
            }
        except ValidationError as exc:
            # Permanent: the file itself is the problem. Do not retry.
            log.warning("task.ingest_rejected", error=exc.message)
            return {"document_id": document_id, "status": "failed", "error": exc.message}
        except SoftTimeLimitExceeded:
            log.error("task.ingest_timeout", document_id=document_id)
            _mark_failed(document_id, "Ingestion exceeded its time limit")
            raise


@celery_app.task(
    bind=True,
    name="documents.reembed",
    autoretry_for=TRANSIENT,
    retry_backoff=True,
    max_retries=3,
)
def reembed_stale_documents(self: Task, model: str, limit: int = 50) -> dict[str, Any]:
    """Re-embed documents whose vectors came from a different model.

    This is the migration path when the embedding model changes: chunks record
    which model produced them, so only the stale ones are recomputed.
    """
    from sqlalchemy import select

    from app.models.document import DocumentChunk

    with sync_session_scope() as session:
        stale_ids = (
            session.execute(
                select(DocumentChunk.document_id)
                .where(
                    (DocumentChunk.embedding_model != model) | (DocumentChunk.embedding.is_(None))
                )
                .distinct()
                .limit(limit)
            )
            .scalars()
            .all()
        )

    log.info("task.reembed_scheduled", count=len(stale_ids), target_model=model)
    for document_id in stale_ids:
        ingest_document.delay(str(document_id))
    return {"scheduled": len(stale_ids), "model": model}


@celery_app.task(
    bind=True,
    name="evaluation.run_suite",
    max_retries=1,
    soft_time_limit=1800,
    time_limit=1900,
)
def run_evaluation_suite(
    self: Task,
    suite: str = "core",
    run_label: str | None = None,
    use_judge: bool = True,
    case_slugs: list[str] | None = None,
) -> dict[str, Any]:
    """Run the evaluation suite off the request path.

    A full sweep is dozens of agent runs; it belongs in a worker with its own
    queue so it cannot starve document ingestion.
    """
    import asyncio

    from app.evaluation.runner import EvaluationRunner

    with LogContext(task_id=self.request.id, suite=suite):
        log.info("task.evaluation_started", suite=suite, use_judge=use_judge)
        summary = asyncio.run(
            EvaluationRunner().run_suite(
                suite=suite, run_label=run_label, use_judge=use_judge, case_slugs=case_slugs
            )
        )
        log.info(
            "task.evaluation_finished",
            suite=suite,
            passed=summary.passed,
            total=summary.total,
            pass_rate=summary.pass_rate,
        )
        return summary.model_dump(mode="json")


@celery_app.task(name="maintenance.expire_approvals")
def expire_stale_approvals() -> dict[str, int]:
    """Mark timed-out approval requests expired.

    A pending approval that nobody ever answers should not sit "awaiting" for
    ever; the run is failed explicitly so the user sees a definite outcome.
    """
    import datetime as dt

    from sqlalchemy import select, update

    from app.models.agent import AgentRun, ApprovalRequest
    from app.models.enums import AgentRunStatus, ApprovalStatus

    now = dt.datetime.now(dt.UTC)
    with sync_session_scope() as session:
        stale = (
            session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                    ApprovalRequest.expires_at < now,
                )
            )
            .scalars()
            .all()
        )
        for approval in stale:
            approval.status = ApprovalStatus.EXPIRED
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == approval.run_id)
                .values(
                    status=AgentRunStatus.FAILED,
                    error_code="approval_expired",
                    error_message="No reviewer decided within the approval window.",
                )
            )
    log.info("task.approvals_expired", count=len(stale))
    return {"expired": len(stale)}


def _mark_failed(document_id: str, message: str) -> None:
    with sync_session_scope() as session:
        document = session.get(Document, uuid.UUID(document_id))
        if document is not None:
            document.ingestion_status = IngestionStatus.FAILED
            document.ingestion_error = message[:2000]
