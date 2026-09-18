"""Structured research endpoints: experiments, compounds, topics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import RateLimitedPrincipal, ResearchServiceDep
from app.auth.dependencies import CurrentPrincipal, require_permissions
from app.auth.permissions import Permission
from app.schemas.common import ErrorResponse, Page
from app.schemas.research import (
    CompoundResponse,
    ExperimentResponse,
    ExperimentResultResponse,
    ResearchTopicDetailResponse,
    ResearchTopicResponse,
)

router = APIRouter(tags=["research"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
}


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentResponse,
    summary="One experiment with its results",
    description=(
        "Accepts a UUID or a human code such as EXP-0007. Returns 403 (not 404) "
        "when the experiment exists but is above the caller's clearance."
    ),
    dependencies=[Depends(require_permissions(Permission.EXPERIMENTS_READ))],
    responses=_NOT_FOUND,
)
async def get_experiment(
    experiment_id: str,
    principal: CurrentPrincipal,
    service: ResearchServiceDep,
) -> ExperimentResponse:
    experiment = await service.get_experiment(experiment_id, principal)
    results = await service.get_experiment_results(experiment.id)
    payload = ExperimentResponse.model_validate(experiment)
    payload.results = [ExperimentResultResponse.model_validate(r) for r in results]
    return payload


@router.get(
    "/experiments/{experiment_id}/results",
    response_model=list[ExperimentResultResponse],
    summary="Measured results for one experiment",
    dependencies=[Depends(require_permissions(Permission.EXPERIMENTS_READ))],
    responses=_NOT_FOUND,
)
async def get_experiment_results(
    experiment_id: str,
    principal: CurrentPrincipal,
    service: ResearchServiceDep,
) -> list[ExperimentResultResponse]:
    experiment = await service.get_experiment(experiment_id, principal)
    results = await service.get_experiment_results(experiment.id)
    return [ExperimentResultResponse.model_validate(r) for r in results]


@router.get(
    "/compounds/{compound_id}",
    response_model=CompoundResponse,
    summary="One compound by UUID or code",
    dependencies=[Depends(require_permissions(Permission.COMPOUNDS_READ))],
    responses=_NOT_FOUND,
)
async def get_compound(
    compound_id: str,
    principal: CurrentPrincipal,
    service: ResearchServiceDep,
) -> CompoundResponse:
    return CompoundResponse.model_validate(await service.get_compound(compound_id))


@router.get(
    "/compounds",
    response_model=Page[CompoundResponse],
    summary="Search compounds",
    dependencies=[Depends(require_permissions(Permission.COMPOUNDS_READ))],
)
async def search_compounds(
    principal: RateLimitedPrincipal,
    service: ResearchServiceDep,
    q: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=50),
) -> Page[CompoundResponse]:
    rows = await service.search_compounds(q, limit=limit)
    return Page[CompoundResponse](
        items=[CompoundResponse.model_validate(c) for c in rows],
        total=len(rows),
        limit=limit,
        offset=0,
    )


@router.get(
    "/research/{topic_id}",
    response_model=ResearchTopicDetailResponse,
    summary="A research topic and its experiments",
    description="Accepts a UUID or a slug such as `breast-cancer-biomarkers`.",
    dependencies=[Depends(require_permissions(Permission.EXPERIMENTS_READ))],
    responses=_NOT_FOUND,
)
async def get_research_topic(
    topic_id: str,
    principal: CurrentPrincipal,
    service: ResearchServiceDep,
) -> ResearchTopicDetailResponse:
    topic = await service.get_topic(topic_id)
    experiments = await service.list_topic_experiments(topic.id, principal)
    payload = ResearchTopicDetailResponse.model_validate(topic)
    payload.experiments = [ExperimentResponse.model_validate(e) for e in experiments]
    return payload


@router.get(
    "/research",
    response_model=list[ResearchTopicResponse],
    summary="List research topics",
    dependencies=[Depends(require_permissions(Permission.EXPERIMENTS_READ))],
)
async def list_topics(
    principal: CurrentPrincipal,
    service: ResearchServiceDep,
) -> list[ResearchTopicResponse]:
    return [ResearchTopicResponse.model_validate(t) for t in await service.list_topics()]
