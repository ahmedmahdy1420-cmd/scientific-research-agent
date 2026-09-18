"""Evaluation API schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class EvaluationCaseResponse(ORMModel):
    id: uuid.UUID
    slug: str
    suite: str
    question: str
    expected_behavior: str
    expected_category: str | None
    expected_tools: list[str]
    forbidden_tools: list[str]
    requires_citations: bool
    requires_approval: bool
    as_role: str
    enabled: bool


class EvaluationResultResponse(ORMModel):
    id: uuid.UUID
    case_id: uuid.UUID
    agent_run_id: uuid.UUID | None
    run_label: str
    passed: bool
    deterministic_scores: dict[str, Any]
    judge_scores: dict[str, Any]
    judge_reasoning: str | None
    overall_score: float
    failures: list[str]
    latency_ms: int
    cost_usd: float
    tools_used: list[str]
    answer: str | None
    human_verdict: str | None
    created_at: dt.datetime


class EvaluationRunRequest(BaseModel):
    suite: str = Field(default="core", max_length=64)
    case_slugs: list[str] | None = None
    use_judge: bool = True
    run_label: str | None = Field(default=None, max_length=128)
    async_mode: bool = Field(
        default=True, description="Dispatch to Celery instead of blocking the request"
    )


class SuiteSummary(BaseModel):
    run_label: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    mean_score: float
    mean_latency_ms: float
    total_cost_usd: float
    by_failure: dict[str, int] = Field(default_factory=dict)


class EvaluationRunResponse(BaseModel):
    run_label: str
    task_id: str | None = None
    summary: SuiteSummary | None = None
    results: list[EvaluationResultResponse] = Field(default_factory=list)


class HumanReviewRequest(BaseModel):
    verdict: str = Field(pattern="^(agree|disagree|unsure)$")
    note: str | None = Field(default=None, max_length=2000)
