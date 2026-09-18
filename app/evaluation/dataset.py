"""Evaluation dataset loading.

Cases live in `data/sample/evaluation_cases.json` (version-controlled, reviewed
like code) and are synced into the `evaluation_cases` table. The table is the
runtime source so that new cases can also be promoted from real production runs
a reviewer marked as wrong — a regression suite that only ever contains cases
someone imagined up front will not catch what actually breaks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.evaluation import EvaluationCase

log = get_logger(__name__)

DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "sample" / "evaluation_cases.json"

_FIELDS = (
    "slug",
    "suite",
    "question",
    "expected_behavior",
    "expected_category",
    "expected_tools",
    "forbidden_tools",
    "expected_tool_arguments",
    "expected_evidence_keywords",
    "expected_answer_keywords",
    "forbidden_answer_keywords",
    "requires_citations",
    "requires_approval",
    "max_latency_ms",
    "max_cost_usd",
    "as_role",
    "enabled",
    "notes",
)


def load_dataset(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or DATASET_PATH
    if not source.exists():
        log.warning("evaluation.dataset_missing", path=str(source))
        return []
    records: list[dict[str, Any]] = json.loads(source.read_text())
    return records


def sync_dataset(session: Session, path: Path | None = None) -> int:
    """Upsert the JSON dataset into the database (sync, used by seeding)."""
    records = load_dataset(path)
    existing = {case.slug: case for case in session.execute(select(EvaluationCase)).scalars().all()}
    for record in records:
        payload = {k: record[k] for k in _FIELDS if k in record}
        case = existing.get(payload["slug"])
        if case is None:
            session.add(EvaluationCase(**payload))
        else:
            for key, value in payload.items():
                setattr(case, key, value)
    session.flush()
    log.info("evaluation.dataset_synced", count=len(records))
    return len(records)


async def get_cases(
    session: AsyncSession,
    *,
    suite: str | None = None,
    slugs: list[str] | None = None,
    enabled_only: bool = True,
) -> list[EvaluationCase]:
    stmt = select(EvaluationCase)
    if suite:
        stmt = stmt.where(EvaluationCase.suite == suite)
    if slugs:
        stmt = stmt.where(EvaluationCase.slug.in_(slugs))
    if enabled_only:
        stmt = stmt.where(EvaluationCase.enabled.is_(True))
    rows = (await session.execute(stmt.order_by(EvaluationCase.slug))).scalars().all()
    return list(rows)
