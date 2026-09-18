"""Evaluation endpoints (the Evaluation Dashboard's backend)."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import case as sql_case
from sqlalchemy import desc, func, select

from app.auth.dependencies import CurrentPrincipal, DbSession, require_permissions
from app.auth.permissions import Permission
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.evaluation.dataset import get_cases
from app.models.evaluation import EvaluationResult
from app.schemas.common import ErrorResponse
from app.schemas.evaluation import (
    EvaluationCaseResponse,
    EvaluationResultResponse,
    EvaluationRunRequest,
    EvaluationRunResponse,
    HumanReviewRequest,
    SuiteSummary,
)

router = APIRouter(prefix="/evaluation", tags=["evaluation"])
log = get_logger(__name__)


@router.get(
    "/cases",
    response_model=list[EvaluationCaseResponse],
    summary="List evaluation cases",
    dependencies=[Depends(require_permissions(Permission.EVALUATION_READ))],
)
async def list_cases(
    principal: CurrentPrincipal,
    session: DbSession,
    suite: str | None = Query(default=None, max_length=64),
) -> list[EvaluationCaseResponse]:
    cases = await get_cases(session, suite=suite, enabled_only=False)
    return [EvaluationCaseResponse.model_validate(c) for c in cases]


@router.post(
    "/run",
    response_model=EvaluationRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run an evaluation suite",
    description=(
        "Dispatches to Celery by default — a full sweep is dozens of agent runs "
        "and does not belong on the request path. Set `async_mode=false` to run "
        "inline (useful in CI on a small suite)."
    ),
    dependencies=[Depends(require_permissions(Permission.EVALUATION_RUN))],
    responses={403: {"model": ErrorResponse}},
)
async def run_suite(
    payload: EvaluationRunRequest,
    principal: CurrentPrincipal,
) -> EvaluationRunResponse:
    label = payload.run_label or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if payload.async_mode:
        from app.workers.tasks import run_evaluation_suite

        task = run_evaluation_suite.delay(
            suite=payload.suite,
            run_label=label,
            use_judge=payload.use_judge,
            case_slugs=payload.case_slugs,
        )
        log.info("evaluation.dispatched", label=label, task_id=task.id, user=principal.user_id)
        return EvaluationRunResponse(run_label=label, task_id=task.id)

    from app.evaluation.runner import EvaluationRunner

    summary = await EvaluationRunner().run_suite(
        suite=payload.suite,
        run_label=label,
        use_judge=payload.use_judge,
        case_slugs=payload.case_slugs,
    )
    return EvaluationRunResponse(run_label=label, summary=summary)


@router.get(
    "/results",
    response_model=list[EvaluationResultResponse],
    summary="Evaluation results, newest first",
    dependencies=[Depends(require_permissions(Permission.EVALUATION_READ))],
)
async def list_results(
    principal: CurrentPrincipal,
    session: DbSession,
    run_label: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[EvaluationResultResponse]:
    stmt = select(EvaluationResult).order_by(desc(EvaluationResult.created_at)).limit(limit)
    if run_label:
        stmt = stmt.where(EvaluationResult.run_label == run_label)
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [EvaluationResultResponse.model_validate(r) for r in rows]


@router.get(
    "/summary",
    response_model=list[SuiteSummary],
    summary="Aggregate pass rate and cost per evaluation run",
    description=(
        "One row per run label. Comparing consecutive labels is the intended "
        "use: absolute judge scores are weakly calibrated, but a drop between "
        "two runs of the same suite is a real regression signal."
    ),
    dependencies=[Depends(require_permissions(Permission.EVALUATION_READ))],
)
async def summary(
    principal: CurrentPrincipal,
    session: DbSession,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[SuiteSummary]:
    stmt = (
        select(
            EvaluationResult.run_label,
            func.count().label("total"),
            func.sum(sql_case((EvaluationResult.passed.is_(True), 1), else_=0)).label("passed"),
            func.avg(EvaluationResult.overall_score).label("mean_score"),
            func.avg(EvaluationResult.latency_ms).label("mean_latency"),
            func.sum(EvaluationResult.cost_usd).label("total_cost"),
            func.max(EvaluationResult.created_at).label("latest"),
        )
        .group_by(EvaluationResult.run_label)
        .order_by(desc(func.max(EvaluationResult.created_at)))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    out: list[SuiteSummary] = []
    for row in rows:
        total = int(row.total or 0)
        passed = int(row.passed or 0)
        out.append(
            SuiteSummary(
                run_label=row.run_label,
                total=total,
                passed=passed,
                failed=total - passed,
                pass_rate=round(passed / total, 4) if total else 0.0,
                mean_score=round(float(row.mean_score or 0.0), 4),
                mean_latency_ms=round(float(row.mean_latency or 0.0), 2),
                total_cost_usd=round(float(row.total_cost or 0.0), 6),
            )
        )
    return out


@router.post(
    "/results/{result_id}/review",
    response_model=EvaluationResultResponse,
    summary="Record a human verdict on a judged result",
    description=(
        "The judge is not ground truth. A reviewer agreeing or disagreeing with "
        "it is recorded here; a rising disagreement rate is how you detect that "
        "the judge prompt or model has drifted."
    ),
    dependencies=[Depends(require_permissions(Permission.EVALUATION_READ))],
)
async def review_result(
    result_id: uuid.UUID,
    payload: HumanReviewRequest,
    principal: CurrentPrincipal,
    session: DbSession,
) -> EvaluationResultResponse:
    result = (
        (await session.execute(select(EvaluationResult).where(EvaluationResult.id == result_id)))
        .unique()
        .scalar_one_or_none()
    )
    if result is None:
        raise NotFoundError(f"No evaluation result with id {result_id}")

    result.human_verdict = payload.verdict
    result.human_note = payload.note
    result.reviewed_at = dt.datetime.now(dt.UTC)
    await session.flush()
    log.info(
        "evaluation.human_review",
        result_id=str(result_id),
        verdict=payload.verdict,
        reviewer=principal.user_id,
    )
    return EvaluationResultResponse.model_validate(result)
