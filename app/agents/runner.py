"""Agent execution: wiring, invocation, interrupt handling.

Owns the boundary between "a graph" and "a request": builds dependencies for
one run, invokes the compiled graph on a thread id, detects an interrupt, and
normalises whatever came back into an `AgentRunResponse`.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.budget import AgentBudget
from app.agents.checkpointer import get_checkpointer
from app.agents.graph import build_agent_graph
from app.agents.nodes import AgentDependencies
from app.agents.state import AgentState, empty_usage, principal_to_state
from app.auth.permissions import Principal
from app.core.config import Settings, get_settings
from app.core.logging import LogContext, get_logger
from app.database.session import get_async_session_factory
from app.llm.base import LLMClient
from app.llm.factory import get_llm_client
from app.models.enums import AgentRunStatus
from app.tools import get_tool_registry
from app.tools.registry import ToolRegistry

log = get_logger(__name__)


@dataclass(slots=True)
class AgentInvocation:
    """Normalised outcome of one graph invocation."""

    state: dict[str, Any]
    interrupted: bool
    interrupt_payload: dict[str, Any] | None
    thread_id: str
    elapsed_ms: int


class AgentRunner:
    """Builds and invokes the agent graph."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        registry: ToolRegistry | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        checkpointer: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm = llm or get_llm_client()
        self._registry = registry or get_tool_registry()
        self._session_factory = session_factory or get_async_session_factory()
        self._checkpointer = checkpointer if checkpointer is not None else get_checkpointer()

    # ------------------------------------------------------------------ init
    def _build(
        self, *, max_iterations: int | None, max_tool_calls: int | None
    ) -> tuple[Any, AgentBudget]:
        budget = AgentBudget.create(
            self._settings,
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
        )
        deps = AgentDependencies(
            llm=self._llm,
            registry=self._registry,
            session_factory=self._session_factory,
            settings=self._settings,
            budget=budget,
        )
        return build_agent_graph(deps, checkpointer=self._checkpointer), budget

    # ------------------------------------------------------------------- run
    async def start(
        self,
        *,
        run_id: uuid.UUID,
        thread_id: str,
        question: str,
        principal: Principal,
        history: list[dict[str, str]] | None = None,
        filters: dict[str, Any] | None = None,
        force_tools: list[str] | None = None,
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
    ) -> AgentInvocation:
        graph, budget = self._build(max_iterations=max_iterations, max_tool_calls=max_tool_calls)
        initial: AgentState = {
            "run_id": str(run_id),
            "question": question,
            "principal": principal_to_state(principal),
            "history": history or [],
            "filters": filters or {},
            "force_tools": force_tools or [],
            "tool_results": [],
            "evidence": [],
            "steps": [],
            "errors": [],
            "usage": empty_usage(),
            "iterations": 0,
            "tool_calls_used": 0,
            "approved_actions": [],
            "status": AgentRunStatus.RUNNING.value,
            "started_at": time.time(),
            "deadline": time.time() + budget.max_runtime_seconds,
        }
        return await self._invoke(graph, initial, thread_id, run_id)

    async def resume(
        self,
        *,
        run_id: uuid.UUID,
        thread_id: str,
        approved: bool,
        decided_by: str,
        note: str | None = None,
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
    ) -> AgentInvocation:
        """Resume a run paused at `approval_gate`."""
        from langgraph.types import Command

        graph, _ = self._build(max_iterations=max_iterations, max_tool_calls=max_tool_calls)
        command: Any = Command(
            resume={"approved": approved, "note": note, "decided_by": decided_by}
        )
        return await self._invoke(graph, command, thread_id, run_id)

    # -------------------------------------------------------------- internal
    async def _invoke(
        self,
        graph: Any,
        payload: Any,
        thread_id: str,
        run_id: uuid.UUID,
    ) -> AgentInvocation:
        config = {
            "configurable": {"thread_id": thread_id},
            # Hard ceiling on supersteps: a belt-and-braces guard against a
            # cycle the budget logic somehow fails to break.
            "recursion_limit": 40,
        }
        started = time.perf_counter()
        with LogContext(agent_run_id=str(run_id), thread_id=thread_id):
            try:
                result = await graph.ainvoke(payload, config=config)
            except Exception as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                log.exception("agent.graph_failed", error=str(exc))
                return AgentInvocation(
                    state={
                        "status": AgentRunStatus.FAILED.value,
                        "errors": [
                            {"node": "graph", "code": "agent_error", "message": str(exc)[:500]}
                        ],
                        "final_answer": {
                            "answer": (
                                "The run failed before an answer could be produced. "
                                "Nothing was inferred in its place."
                            ),
                            "citations": [],
                            "confidence": "low",
                            "caveats": ["Internal agent failure."],
                            "unanswered_aspects": [],
                        },
                        "steps": [],
                        "usage": empty_usage(),
                    },
                    interrupted=False,
                    interrupt_payload=None,
                    thread_id=thread_id,
                    elapsed_ms=elapsed,
                )

        elapsed = int((time.perf_counter() - started) * 1000)
        interrupts = result.get("__interrupt__") or []
        if interrupts:
            first = interrupts[0]
            payload_value = getattr(first, "value", None) or {}
            log.info("agent.interrupted", run_id=str(run_id), action=payload_value.get("action"))
            return AgentInvocation(
                state=dict(result),
                interrupted=True,
                interrupt_payload={
                    "interrupt_id": getattr(first, "id", None),
                    **payload_value,
                },
                thread_id=thread_id,
                elapsed_ms=elapsed,
            )

        return AgentInvocation(
            state=dict(result),
            interrupted=False,
            interrupt_payload=None,
            thread_id=thread_id,
            elapsed_ms=elapsed,
        )

    async def get_state(self, thread_id: str) -> dict[str, Any]:
        """Read the checkpointed state for a thread (used for run details)."""
        graph, _ = self._build(max_iterations=None, max_tool_calls=None)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values) if snapshot and snapshot.values else {}


def new_thread_id() -> str:
    return f"run-{uuid.uuid4().hex[:16]}"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
