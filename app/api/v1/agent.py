"""Chat and agent endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, status

from app.api.deps import AgentRateLimitedPrincipal, AgentServiceDep
from app.auth.dependencies import CurrentPrincipal, RequireApprovalDecision
from app.core.logging import get_logger
from app.schemas.agent import (
    AgentRunRequest,
    AgentRunResponse,
    AgentRunSummary,
    ApprovalDecisionRequest,
    ChatRequest,
)
from app.schemas.common import ErrorResponse, Page

router = APIRouter(tags=["agent"])
log = get_logger(__name__)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


@router.post(
    "/chat",
    response_model=AgentRunResponse,
    summary="Ask the research assistant a question",
    description=(
        "Runs the full agent: classify, plan, authorise and execute tools, "
        "synthesise, verify, answer with citations. Returns 200 with an answer, "
        "or a run in `awaiting_approval` status when the question requires a "
        "sensitive action."
    ),
    responses=_ERRORS,
)
async def chat(
    payload: ChatRequest,
    principal: AgentRateLimitedPrincipal,
    service: AgentServiceDep,
) -> AgentRunResponse:
    request = AgentRunRequest(**payload.model_dump())
    return await service.run(request, principal)


@router.post(
    "/agent/run",
    response_model=AgentRunResponse,
    summary="Run the agent with explicit execution bounds",
    description=(
        "Same pipeline as /chat, with the iteration and tool-call ceilings "
        "exposed. Values are still clamped server-side: a client cannot raise "
        "the budget above the deployment's configured maximum."
    ),
    responses=_ERRORS,
)
async def run_agent(
    payload: AgentRunRequest,
    principal: AgentRateLimitedPrincipal,
    service: AgentServiceDep,
) -> AgentRunResponse:
    return await service.run(payload, principal)


@router.get(
    "/agent/runs",
    response_model=Page[AgentRunSummary],
    summary="List agent runs visible to the caller",
)
async def list_runs(
    principal: CurrentPrincipal,
    service: AgentServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[AgentRunSummary]:
    rows, total = await service.list_runs(principal, limit=limit, offset=offset)
    return Page[AgentRunSummary](
        items=[AgentRunSummary.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/agent/runs/{run_id}",
    response_model=AgentRunResponse,
    summary="Full detail of one agent run",
    description=(
        "Plan, every tool call with arguments and latency, the evidence used, "
        "the verification verdict and the cost breakdown."
    ),
    responses={404: {"model": ErrorResponse}, **_ERRORS},
)
async def get_run(
    run_id: uuid.UUID,
    principal: CurrentPrincipal,
    service: AgentServiceDep,
) -> AgentRunResponse:
    return await service.get_run_response(run_id, principal)


@router.post(
    "/agent/runs/{run_id}/approve",
    response_model=AgentRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve or reject a paused agent run",
    description=(
        "Human-in-the-loop decision point. Requires the `approvals:decide` "
        "permission, which researchers do not have. On approval the run resumes "
        "from its checkpoint and executes the sensitive action; on rejection it "
        "terminates without acting. Both outcomes are recorded with the "
        "deciding user and timestamp."
    ),
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, **_ERRORS},
)
async def decide_approval(
    run_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    principal: RequireApprovalDecision,
    service: AgentServiceDep,
) -> AgentRunResponse:
    return await service.decide_approval(
        run_id, approved=payload.approved, note=payload.note, principal=principal
    )
