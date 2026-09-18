"""Celery application.

Configuration choices that matter in production:

* `task_acks_late=True` with `worker_prefetch_multiplier=1` — a task is only
  acknowledged after it completes, so a worker killed mid-ingestion (spot
  reclaim, deploy, OOM) returns the task to the queue instead of losing it.
  That is only safe because the ingestion pipeline is idempotent.
* `task_reject_on_worker_lost=True` — same guarantee for hard crashes.
* `task_time_limit` / `soft_time_limit` — a wedged PDF cannot occupy a worker
  forever; the soft limit raises inside the task so it can record `failed`.
* Separate queues (`ingestion`, `evaluation`, `default`) so a long evaluation
  sweep cannot starve user-triggered document ingestion.
* `broker_connection_retry_on_startup` — workers start before Redis is ready
  during a rolling deploy and must not crash-loop.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging, task_failure, task_retry, task_success

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

settings = get_settings()
log = get_logger(__name__)

celery_app = Celery(
    "scientific_research_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=900,
    task_soft_time_limit=840,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    worker_max_tasks_per_child=100,  # bounds any slow leak in PDF parsing
    task_default_queue="default",
    task_routes={
        "documents.ingest": {"queue": "ingestion"},
        "documents.reembed": {"queue": "ingestion"},
        "evaluation.run_suite": {"queue": "evaluation"},
    },
    # A pending approval that nobody ever answers must not sit "awaiting"
    # forever: the beat schedule expires it so the user gets a definite outcome.
    beat_schedule={
        "expire-stale-approvals": {
            "task": "maintenance.expire_approvals",
            "schedule": 300.0,
            "options": {"queue": "default", "expires": 240},
        },
    },
)


@setup_logging.connect
def _configure_celery_logging(**_kwargs: object) -> None:
    """Use structlog inside workers too, so log shipping stays uniform."""
    configure_logging(settings.log_level, settings.log_format)


@task_success.connect
def _on_success(sender: object = None, **_kwargs: object) -> None:
    log.info("celery.task_succeeded", task=getattr(sender, "name", "unknown"))


@task_retry.connect
def _on_retry(sender: object = None, reason: object = None, **_kwargs: object) -> None:
    log.warning(
        "celery.task_retry", task=getattr(sender, "name", "unknown"), reason=str(reason)[:300]
    )


@task_failure.connect
def _on_failure(
    sender: object = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    **_kwargs: object,
) -> None:
    log.error(
        "celery.task_failed",
        task=getattr(sender, "name", "unknown"),
        task_id=task_id,
        error=str(exception)[:500],
    )
