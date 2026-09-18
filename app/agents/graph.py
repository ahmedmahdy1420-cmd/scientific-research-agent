"""The LangGraph state machine.

    START
      v
    classify_question
      |-- small talk / out of scope ------------------> direct_answer --> END
      |-- sensitive ---------------------------------> approval_gate
      v                                                   |  (interrupt: waits
    plan                                                  |   for a human)
      v                                        approved / rejected
    select_tools                                          |
      |  (fan out to whichever groups the plan needs)     |
      +--> rag_tools ------+                              |
      +--> sql_tools ------+--> analyze <----------------+
      +--> external_tools -+       v
                                 verify
                                   |-- accept / caveats --> finalize --> END
                                   |-- retrieve_more ------> retrieve_more
                                   |        (bounded by MAX_AGENT_ITERATIONS)
                                   |              back to the tool fan-out
                                   +-- refuse -------------> finalize --> END

Why LangGraph and not a while-loop with function calls:

* the control flow is **explicit and inspectable** - the branches above are
  data, so the UI can render exactly which path a run took;
* **checkpointing** is built in, which is what makes `interrupt()` (and
  therefore human approval across process restarts) work at all;
* **fan-out/fan-in with reducers** handles concurrent tool groups without
  hand-rolled gather/merge code;
* retries, recursion limits and per-node timeouts are framework concerns rather
  than things every node re-implements.

A plain loop is genuinely fine for a single-tool assistant. It stops being fine
the moment you need to pause for a human and resume later.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.agents.nodes import AgentDependencies, AgentNodes
from app.agents.state import AgentState
from app.core.logging import get_logger
from app.models.enums import AgentRunStatus, QuestionCategory

log = get_logger(__name__)

NO_TOOL_CATEGORIES = {QuestionCategory.SMALL_TALK.value, QuestionCategory.OUT_OF_SCOPE.value}


# =============================================================================
# Routers (pure functions of state - trivially unit-testable)
# =============================================================================
def route_after_classify(state: AgentState) -> Literal["plan", "direct_answer"]:
    """Only small talk skips planning.

    A sensitive request still goes through `plan` -> `select_tools`: the planner
    is what decides WHICH allowlisted action is being requested, and
    `select_tools` is what authorises it. Jumping straight to the approval gate
    would mean asking a human to approve an action nobody had validated yet.
    """
    if state.get("category") in NO_TOOL_CATEGORIES:
        return "direct_answer"
    return "plan"


def route_tools(state: AgentState) -> list[str]:
    """Fan out to exactly the tool groups this plan needs.

    Returning a list from a conditional edge starts those nodes in parallel.
    An empty plan still has to reach `analyze`, which will then produce an
    honest "no evidence" answer rather than hanging.
    """
    if state.get("status") == AgentRunStatus.BUDGET_EXCEEDED.value:
        return ["finalize"]

    groups = {call.get("group") for call in state.get("pending_tool_calls") or []}

    # A sensitive call short-circuits everything else: pause for a human before
    # running any tool at all, so an approved run cannot already have acted.
    if "sensitive" in groups:
        return ["approval_gate"]

    branches = [f"{g}_tools" for g in ("rag", "sql", "external") if g in groups]
    return branches or ["analyze"]


def route_after_approval(state: AgentState) -> Literal["sensitive_tools", "finalize"]:
    if state.get("status") == AgentRunStatus.REJECTED.value:
        return "finalize"
    return "sensitive_tools"


def route_after_verify(state: AgentState) -> Literal["retrieve_more", "finalize"]:
    """Pure routing decision, ignoring budget.

    The compiled graph uses `_bounded_verify_router`, which wraps this with the
    iteration ceiling. This version exists so the branch logic can be unit
    tested without constructing a budget.
    """
    if state.get("status") in (
        AgentRunStatus.FAILED.value,
        AgentRunStatus.BUDGET_EXCEEDED.value,
    ):
        return "finalize"
    verification = state.get("verification") or {}
    if verification.get("recommendation") == "retrieve_more":
        return "retrieve_more"
    return "finalize"


def route_after_analyze(state: AgentState) -> Literal["verify", "handle_failure"]:
    if state.get("status") == AgentRunStatus.FAILED.value:
        return "handle_failure"
    return "verify"


# =============================================================================
# Builder
# =============================================================================
def build_agent_graph(deps: AgentDependencies, checkpointer: Any = None) -> Any:
    """Compile the graph. `checkpointer` is required for human-in-the-loop."""
    nodes = AgentNodes(deps)
    builder: StateGraph[AgentState] = StateGraph(AgentState)

    builder.add_node("classify_question", nodes.classify_question)
    builder.add_node("plan", nodes.plan)
    builder.add_node("select_tools", nodes.select_tools)
    builder.add_node("rag_tools", nodes.rag_tools)
    builder.add_node("sql_tools", nodes.sql_tools)
    builder.add_node("external_tools", nodes.external_tools)
    builder.add_node("sensitive_tools", nodes.sensitive_tools)
    builder.add_node("analyze", nodes.analyze)
    builder.add_node("verify", nodes.verify)
    builder.add_node("retrieve_more", nodes.retrieve_more)
    builder.add_node("approval_gate", nodes.approval_gate)
    builder.add_node("direct_answer", nodes.direct_answer)
    builder.add_node("finalize", nodes.finalize)
    builder.add_node("handle_failure", nodes.handle_failure)

    builder.add_edge(START, "classify_question")
    builder.add_conditional_edges(
        "classify_question",
        route_after_classify,
        ["plan", "direct_answer"],
    )

    builder.add_edge("plan", "select_tools")
    builder.add_conditional_edges(
        "select_tools",
        route_tools,
        [
            "rag_tools",
            "sql_tools",
            "external_tools",
            "approval_gate",
            "analyze",
            "finalize",
        ],
    )

    # Fan-in: whichever branches ran, they converge on analyze.
    builder.add_edge("rag_tools", "analyze")
    builder.add_edge("sql_tools", "analyze")
    builder.add_edge("external_tools", "analyze")

    builder.add_conditional_edges("analyze", route_after_analyze, ["verify", "handle_failure"])
    builder.add_conditional_edges(
        "verify", _bounded_verify_router(deps), ["retrieve_more", "finalize"]
    )

    # The retrieval loop: back to the RAG branch, never past the budget check.
    builder.add_edge("retrieve_more", "rag_tools")

    # Human-in-the-loop branch.
    builder.add_conditional_edges(
        "approval_gate", route_after_approval, ["sensitive_tools", "finalize"]
    )
    # A completed sensitive action is reported directly; there is no evidence
    # corpus to synthesise from, and running it through `analyze` would
    # produce a spurious "no evidence found".
    builder.add_edge("sensitive_tools", "finalize")

    builder.add_edge("direct_answer", END)
    builder.add_edge("finalize", END)
    builder.add_edge("handle_failure", END)

    return builder.compile(
        checkpointer=checkpointer,
        name="scientific-research-agent",
    )


def _bounded_verify_router(deps: AgentDependencies) -> Any:
    """Close over the budget so the router can enforce the iteration ceiling
    without smuggling a non-serialisable object into the checkpointed state."""

    def router(state: AgentState) -> str:
        if state.get("status") in (
            AgentRunStatus.FAILED.value,
            AgentRunStatus.BUDGET_EXCEEDED.value,
        ):
            return "finalize"

        verification = state.get("verification") or {}
        if verification.get("recommendation") != "retrieve_more":
            return "finalize"

        # Stop if the previous retry produced no new evidence. Re-running a
        # query that already returned nothing will return nothing again; the
        # only thing another lap buys is latency and tokens.
        seen: set[str] = set()
        for item in state.get("evidence") or []:
            if item.get("source_id"):
                seen.add(item["source_id"])
        before = state.get("evidence_before_retry")
        if before is not None and len(seen) <= before:
            log.info(
                "agent.retrieval_loop_stopped",
                run_id=state.get("run_id"),
                reason="previous retry added no new evidence",
            )
            return "finalize"

        verdict = deps.budget.check(
            iterations=state.get("iterations", 0),
            tool_calls_used=state.get("tool_calls_used", 0),
        )
        if not verdict.ok:
            log.info(
                "agent.retrieval_loop_stopped",
                run_id=state.get("run_id"),
                reason=verdict.reason,
            )
            return "finalize"
        return "retrieve_more"

    return router


def render_mermaid() -> str:
    """Mermaid source for docs/architecture/agent-workflow.md."""
    return """flowchart TD
    START([START]) --> CLASSIFY[classify_question]
    CLASSIFY -->|small talk / out of scope| DIRECT[direct_answer]
    CLASSIFY -->|research question| PLAN[plan]
    PLAN --> SELECT[select_tools]
    SELECT -->|sensitive action planned| APPROVAL[approval_gate]
    SELECT -->|needs documents| RAG[rag_tools]
    SELECT -->|needs experiments| SQL[sql_tools]
    SELECT -->|needs trials / MCP| EXT[external_tools]
    SELECT -->|nothing authorised| ANALYZE
    RAG --> ANALYZE[analyze]
    SQL --> ANALYZE
    EXT --> ANALYZE
    ANALYZE --> VERIFY[verify]
    ANALYZE -->|synthesis failed| FAIL[handle_failure]
    VERIFY -->|insufficient, budget left| MORE[retrieve_more]
    MORE --> RAG
    VERIFY -->|accept / caveats / refuse| FINAL[finalize]
    APPROVAL -->|approved| SENS[sensitive_tools]
    APPROVAL -->|rejected| FINAL
    SENS --> FINAL
    DIRECT --> END([END])
    FINAL --> END
    FAIL --> END"""
