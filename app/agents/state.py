"""Typed agent state.

Two rules this state obeys, both learned the hard way:

1. **Everything in it is JSON-serialisable.** The state is checkpointed to
   Postgres so a run can pause for human approval and resume in a different
   process (or a different container) minutes later. A live `AsyncSession` or a
   `Principal` dataclass in here would not survive that round trip, so the
   principal travels as a dict and is rehydrated by each node.
2. **Any key written by more than one node running in parallel has a reducer.**
   The three tool-executor nodes run concurrently; without `operator.add` on
   `tool_results`, LangGraph raises `InvalidUpdateError` on the concurrent write.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.auth.permissions import Principal


def merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Sum usage counters across concurrent branches."""
    if not left:
        return dict(right or {})
    if not right:
        return dict(left)
    merged = dict(left)
    for key, value in right.items():
        if key == "models_used":
            merged[key] = sorted({*(left.get(key) or []), *(value or [])})
        elif isinstance(value, int | float):
            merged[key] = (merged.get(key) or 0) + value
        else:
            merged[key] = value
    return merged


class AgentState(TypedDict, total=False):
    """The full working memory of one agent run."""

    # --- inputs -------------------------------------------------------------
    run_id: str
    question: str
    principal: dict[str, Any]
    history: list[dict[str, str]]
    filters: dict[str, Any]
    #: Caller-supplied tool override (see /agent/run). Acts as an allowlist AND
    #: guarantees at least one call per named tool.
    force_tools: list[str]

    # --- classification / planning ------------------------------------------
    category: str
    classification: dict[str, Any]
    plan: dict[str, Any] | None

    # --- tool execution -----------------------------------------------------
    pending_tool_calls: list[dict[str, Any]]
    tool_results: Annotated[list[dict[str, Any]], operator.add]
    evidence: Annotated[list[dict[str, Any]], operator.add]

    #: Evidence count at the moment the last retrieval retry was launched.
    evidence_before_retry: int

    # --- synthesis ----------------------------------------------------------
    draft_answer: dict[str, Any] | None
    verification: dict[str, Any] | None
    final_answer: dict[str, Any] | None

    # --- human in the loop --------------------------------------------------
    approval: dict[str, Any] | None
    approved_actions: list[str]

    # --- bookkeeping --------------------------------------------------------
    status: str
    iterations: int
    tool_calls_used: Annotated[int, operator.add]
    usage: Annotated[dict[str, Any], merge_usage]
    steps: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
    started_at: float
    deadline: float


def principal_from_state(state: AgentState) -> Principal:
    """Rehydrate the caller from checkpointed state."""
    raw = state.get("principal") or {}
    return Principal(
        user_id=raw.get("user_id", ""),
        email=raw.get("email", ""),
        full_name=raw.get("full_name", ""),
        roles=frozenset(raw.get("roles", [])),
        permissions=frozenset(raw.get("permissions", [])),
        department=raw.get("department"),
    )


def principal_to_state(principal: Principal) -> dict[str, Any]:
    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "full_name": principal.full_name,
        "roles": sorted(principal.roles),
        "permissions": sorted(principal.permissions),
        "department": principal.department,
    }


def empty_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost_usd": 0.0,
        "llm_latency_ms": 0,
        "tool_latency_ms": 0,
        "retrieval_latency_ms": 0,
        "cache_hits": 0,
        "models_used": [],
    }
