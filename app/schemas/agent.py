"""Agent-facing structured output contracts.

These Pydantic models are the *interface between the model and the program*.
Everything the LLM produces that the application then acts on goes through one
of them, so a malformed or hallucinated response is a validation error at a
known boundary rather than a surprise three layers down.

Models in the "LLM-facing" section deliberately avoid defaults and use
`X | None` for optional fields: OpenAI structured outputs run in strict mode,
where every property must appear in `required`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import QuestionCategory
from app.schemas.common import ORMModel

ToolName = Literal[
    "retrieve_documents",
    "search_literature",
    "search_clinical_trials",
    "get_experiment",
    "get_experiment_results",
    "search_compounds",
    "search_experiments_by_compound",
    "mcp_search_research_data",
    "mcp_get_trial_information",
    "request_sensitive_action",
]


# =============================================================================
# LLM-facing structured outputs
# =============================================================================
class QuestionClassification(BaseModel):
    """Cheap first hop: run on the fast model to pick a branch of the graph."""

    model_config = ConfigDict(extra="forbid")

    category: QuestionCategory
    reasoning: str = Field(description="One sentence on why this category")
    requires_tools: bool
    is_sensitive: bool = Field(
        description="True if the request would modify data or trigger a real-world action"
    )
    entities: list[str] = Field(description="Compounds, conditions, experiment codes mentioned")


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: ToolName
    rationale: str
    arguments: dict[str, Any] = Field(
        description="Arguments for the tool; validated against the tool's own schema before use"
    )


class AgentPlan(BaseModel):
    """What the model proposes to do. A proposal, not an instruction."""

    model_config = ConfigDict(extra="forbid")

    objective: str
    steps: list[PlannedStep] = Field(max_length=8)
    needs_human_approval: bool
    expected_evidence: str = Field(description="What good evidence for this question looks like")

    @field_validator("steps")
    @classmethod
    def _cap_steps(cls, v: list[PlannedStep]) -> list[PlannedStep]:
        return v[:8]


class ToolCallRequest(BaseModel):
    """A tool call the model asked for. The backend decides whether it runs."""

    model_config = ConfigDict(extra="forbid")

    tool: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class EvidenceItem(BaseModel):
    """One citable fact. `source_id` must point at something that really exists."""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["document", "experiment", "clinical_trial", "compound"]
    source_id: str
    title: str
    snippet: str
    #: Provenance for the UI: which tool produced this.
    origin_tool: str | None = None
    relevance_score: float | None = None
    publication_date: dt.date | None = None
    url: str | None = None


class ResearchAnswer(BaseModel):
    """Final synthesis. Citations are structurally required, not merely asked for."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[str] = Field(
        description="source_id values from the supplied evidence, verbatim"
    )
    confidence: Literal["high", "medium", "low"]
    caveats: list[str]
    unanswered_aspects: list[str] = Field(
        description="Parts of the question the evidence does not cover"
    )


class VerificationResult(BaseModel):
    """Grounding check performed *after* synthesis, before the answer is returned."""

    model_config = ConfigDict(extra="forbid")

    grounded: bool = Field(description="Every claim traceable to supplied evidence")
    sufficient: bool = Field(description="Evidence covers the question")
    unsupported_claims: list[str]
    invalid_citations: list[str]
    missing_information: str | None
    recommendation: Literal["accept", "retrieve_more", "answer_with_caveats", "refuse"]
    reasoning: str


# =============================================================================
# API request/response models
# =============================================================================
class ChatRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    #: Prior turns, oldest first. Kept small: this is not a memory system.
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    research_area: str | None = Field(default=None, max_length=128)
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    stream: bool = False


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class AgentRunRequest(ChatRequest):
    """`/agent/run` is `/chat` with the execution knobs exposed."""

    max_iterations: int | None = Field(default=None, ge=1, le=5)
    max_tool_calls: int | None = Field(default=None, ge=1, le=20)
    force_tools: list[ToolName] | None = None


class ToolCallRecord(ORMModel):
    id: uuid.UUID
    tool_name: str
    status: str
    arguments: dict[str, Any]
    result_summary: str | None = None
    result_count: int | None = None
    denial_reason: str | None = None
    error_code: str | None = None
    latency_ms: int
    retry_count: int
    cache_hit: bool
    created_at: dt.datetime


class AgentStep(BaseModel):
    """One node execution, for the run-details timeline in the UI."""

    node: str
    sequence: int
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    started_at: dt.datetime | None = None
    duration_ms: int | None = None


class UsageStats(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    llm_latency_ms: int = 0
    tool_latency_ms: int = 0
    retrieval_latency_ms: int = 0
    total_latency_ms: int = 0
    iterations: int = 0
    tool_calls: int = 0
    models_used: list[str] = Field(default_factory=list)
    cache_hits: int = 0


class ApprovalPrompt(BaseModel):
    """Returned when a run pauses for a human decision."""

    approval_id: uuid.UUID
    action: str
    reason: str
    risk_level: str
    payload: dict[str, Any]
    expires_at: dt.datetime | None = None


class AgentRunResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    question: str
    answer: str | None = None
    confidence: str | None = None
    category: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    plan: AgentPlan | None = None
    steps: list[AgentStep] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    verification: VerificationResult | None = None
    approval: ApprovalPrompt | None = None
    usage: UsageStats = Field(default_factory=UsageStats)
    error: str | None = None
    created_at: dt.datetime | None = None


class AgentRunSummary(ORMModel):
    id: uuid.UUID
    question: str
    status: str
    category: str | None
    created_at: dt.datetime
    total_latency_ms: int
    estimated_cost_usd: float
    tool_calls_used: int


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=1000)


ChatRequest.model_rebuild()
AgentRunRequest.model_rebuild()
