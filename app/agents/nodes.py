"""Agent graph nodes.

Each node is a small, testable async function: state in, state delta out. No
node raises into the graph — a failure becomes an `errors` entry and a status,
because an exception escaping a node loses the run's partial results and the
audit trail with it.

Tool groups exist so that independent lookups actually run in parallel. A
question that needs both literature and trial data pays for one round trip, not
two, and the fan-out is visible in the graph rather than hidden inside a node.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.budget import AgentBudget, deadline_exceeded
from app.agents.state import AgentState, empty_usage, principal_from_state
from app.core.config import Settings
from app.core.errors import AppError, LLMError, LLMOutputValidationError
from app.core.logging import get_logger
from app.llm.base import LLMClient, Message, Usage
from app.llm.prompts import (
    CLASSIFIER_SYSTEM,
    DIRECT_ANSWER_SYSTEM,
    PLANNER_SYSTEM,
    SYNTHESIS_SYSTEM,
    VERIFIER_SYSTEM,
    build_evidence_block,
    build_user_question_block,
    principal_context,
)
from app.models.enums import AgentRunStatus, QuestionCategory
from app.schemas.agent import (
    AgentPlan,
    EvidenceItem,
    QuestionClassification,
    ResearchAnswer,
    VerificationResult,
)
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import ToolRegistry

log = get_logger(__name__)

# --- tool groups -> graph branches ------------------------------------------
RAG_TOOLS = frozenset({"retrieve_documents", "search_literature"})
SQL_TOOLS = frozenset(
    {
        "get_experiment",
        "get_experiment_results",
        "search_compounds",
        "search_experiments_by_compound",
    }
)
EXTERNAL_TOOLS = frozenset(
    {"search_clinical_trials", "mcp_search_research_data", "mcp_get_trial_information"}
)
SENSITIVE_GROUP = frozenset({"request_sensitive_action"})

#: For `force_tools`: which argument carries the user's question when the
#: planner did not produce a call for a tool the caller insisted on. Tools that
#: need a specific identifier (an experiment code, a registry id) are absent on
#: purpose - we will not invent one.
FORCED_TOOL_PRIMARY_ARG: dict[str, str] = {
    "retrieve_documents": "query",
    "search_literature": "query",
    "search_clinical_trials": "condition",
    "search_compounds": "query",
    "search_experiments_by_compound": "compound_query",
    "mcp_search_research_data": "query",
}


def tool_group(name: str) -> str:
    if name in RAG_TOOLS:
        return "rag"
    if name in SQL_TOOLS:
        return "sql"
    if name in EXTERNAL_TOOLS:
        return "external"
    if name in SENSITIVE_GROUP:
        return "sensitive"
    return "unknown"


@dataclass(slots=True)
class AgentDependencies:
    """Everything the nodes need, injected so tests can supply fakes."""

    llm: LLMClient
    registry: ToolRegistry
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    budget: AgentBudget


def _step(node: str, summary: str, **detail: Any) -> dict[str, Any]:
    return {
        "node": node,
        "summary": summary,
        "detail": detail,
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }


def _usage_delta(usage: Usage, *, bucket: str = "llm_latency_ms") -> dict[str, Any]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "estimated_cost_usd": usage.cost_usd,
        bucket: usage.latency_ms,
        "models_used": [usage.model] if usage.model else [],
    }


class AgentNodes:
    """The node implementations, bound to a set of dependencies."""

    def __init__(self, deps: AgentDependencies) -> None:
        self.deps = deps

    # ------------------------------------------------------------------ util
    def _tool_context(self, state: AgentState) -> ToolContext:
        return ToolContext(
            principal=principal_from_state(state),
            session_factory=self.deps.session_factory,
            run_id=uuid.UUID(state["run_id"]) if state.get("run_id") else None,
            approved_actions=frozenset(state.get("approved_actions") or []),
        )

    @staticmethod
    def _collected_evidence(state: AgentState) -> list[dict[str, Any]]:
        """Deduplicate evidence by source_id, preserving first-seen order."""
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in state.get("evidence") or []:
            sid = item.get("source_id")
            if sid and sid not in seen:
                seen.add(sid)
                unique.append(item)
        return unique

    def _evidence_block(self, evidence: list[dict[str, Any]]) -> tuple[str, set[str]]:
        lines = [
            f"[{item['source_id']}] {item.get('title', '')} :: {item.get('snippet', '')}"
            for item in evidence
        ]
        return build_evidence_block(lines), {item["source_id"] for item in evidence}

    # =================================================== 1. classify ========
    async def classify_question(self, state: AgentState) -> dict[str, Any]:
        """Cheap routing decision on the fast model."""
        started = time.perf_counter()
        principal = principal_from_state(state)
        messages = [
            Message(role="system", content=CLASSIFIER_SYSTEM),
            Message(role="user", content=build_user_question_block(state["question"])),
        ]
        try:
            result = await self.deps.llm.structured_output(
                messages, QuestionClassification, task="fast"
            )
        except (LLMError, LLMOutputValidationError) as exc:
            # Classification is a routing hint, not the answer. Degrade to the
            # most generally useful branch rather than failing the run.
            log.warning("agent.classify_failed", error=exc.message, run_id=state.get("run_id"))
            return {
                "category": QuestionCategory.LITERATURE.value,
                "classification": {"category": "literature", "fallback": True},
                "errors": [{"node": "classify_question", "code": exc.code, "message": exc.message}],
                "steps": [
                    _step("classify_question", "Classifier failed; defaulted to literature.")
                ],
                "usage": empty_usage(),
            }

        classification: QuestionClassification = result.parsed
        log.info(
            "agent.classified",
            run_id=state.get("run_id"),
            category=classification.category.value,
            sensitive=classification.is_sensitive,
            user_id=principal.user_id,
        )
        return {
            "category": classification.category.value,
            "classification": classification.model_dump(mode="json"),
            "usage": _usage_delta(result.usage),
            "steps": [
                _step(
                    "classify_question",
                    f"Category: {classification.category.value}",
                    reasoning=classification.reasoning,
                    entities=classification.entities,
                    sensitive=classification.is_sensitive,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
        }

    # =================================================== 2. plan ============
    async def plan(self, state: AgentState) -> dict[str, Any]:
        """Ask the reasoning model for a plan of typed tool calls."""
        principal = principal_from_state(state)
        available = ", ".join(t.name for t in self.deps.registry.schemas_for(principal))
        context_lines = [
            principal_context(principal),
            f"CATEGORY: {state.get('category', 'literature')}",
            f"TOOLS AVAILABLE TO THIS CALLER: {available}",
            build_user_question_block(state["question"]),
        ]
        messages = [
            Message(role="system", content=PLANNER_SYSTEM),
            Message(role="user", content="\n\n".join(context_lines)),
        ]
        try:
            result = await self.deps.llm.structured_output(messages, AgentPlan, task="reasoning")
        except (LLMError, LLMOutputValidationError) as exc:
            log.warning("agent.plan_failed", error=exc.message, run_id=state.get("run_id"))
            # Fall back to the single most useful tool rather than giving up.
            fallback = AgentPlan(
                objective=state["question"][:300],
                steps=[
                    {
                        "tool": "retrieve_documents",
                        "rationale": "Planner unavailable; default to corpus retrieval.",
                        "arguments": {"query": state["question"][:200], "limit": 8},
                    }
                ],
                needs_human_approval=False,
                expected_evidence="Passages relevant to the question.",
            )
            return {
                "plan": fallback.model_dump(mode="json"),
                "errors": [{"node": "plan", "code": exc.code, "message": exc.message}],
                "steps": [_step("plan", "Planner failed; using default retrieval plan.")],
                "usage": empty_usage(),
            }

        agent_plan: AgentPlan = result.parsed
        log.info(
            "agent.planned",
            run_id=state.get("run_id"),
            steps=[s.tool for s in agent_plan.steps],
            needs_approval=agent_plan.needs_human_approval,
        )
        return {
            "plan": agent_plan.model_dump(mode="json"),
            "usage": _usage_delta(result.usage),
            "steps": [
                _step(
                    "plan",
                    f"Planned {len(agent_plan.steps)} step(s): "
                    f"{', '.join(s.tool for s in agent_plan.steps) or 'none'}",
                    objective=agent_plan.objective,
                    expected_evidence=agent_plan.expected_evidence,
                )
            ],
        }

    # =================================================== 3. select_tools ====
    async def select_tools(self, state: AgentState) -> dict[str, Any]:
        """Turn the model's plan into an authorised, budgeted call list.

        This is where the plan stops being a suggestion. Unknown tools are
        dropped, tools the caller may not use are dropped, and the list is
        truncated to the remaining tool-call budget.
        """
        principal = principal_from_state(state)
        plan = state.get("plan") or {}
        verdict = self.deps.budget.check(
            iterations=state.get("iterations", 0),
            tool_calls_used=state.get("tool_calls_used", 0),
        )
        if not verdict.ok:
            return {
                "pending_tool_calls": [],
                "status": AgentRunStatus.BUDGET_EXCEEDED.value,
                "steps": [_step("select_tools", f"Budget stop: {verdict.reason}")],
            }

        forced = list(state.get("force_tools") or [])
        selected: list[dict[str, Any]] = []
        dropped: list[dict[str, str]] = []

        for step in plan.get("steps", []):
            # `force_tools` is an allowlist as well as a guarantee: a caller
            # pinning the tool set should not also get the planner's picks.
            if forced and step.get("tool") not in forced:
                dropped.append({"tool": str(step.get("tool")), "reason": "not in force_tools"})
                continue
            name = step.get("tool")
            tool = self.deps.registry.get(name) if name else None
            if tool is None:
                dropped.append({"tool": str(name), "reason": "unknown tool"})
                continue
            if tool.required_permission and not principal.has_permission(tool.required_permission):
                dropped.append({"tool": name, "reason": "not permitted for this caller"})
                continue
            if len(selected) >= verdict.remaining_tool_calls:
                dropped.append({"tool": name, "reason": "tool-call budget"})
                continue
            selected.append(
                {
                    "tool": name,
                    "arguments": step.get("arguments") or {},
                    "group": tool_group(name),
                }
            )

        # Add any forced tool the planner did not pick, provided we can build
        # its arguments honestly from the question.
        for name in forced:
            if any(call["tool"] == name for call in selected):
                continue
            tool = self.deps.registry.get(name)
            primary = FORCED_TOOL_PRIMARY_ARG.get(name)
            if tool is None or primary is None:
                dropped.append(
                    {"tool": name, "reason": "forced tool needs arguments we cannot infer"}
                )
                continue
            if tool.required_permission and not principal.has_permission(tool.required_permission):
                dropped.append({"tool": name, "reason": "not permitted for this caller"})
                continue
            if len(selected) >= verdict.remaining_tool_calls:
                dropped.append({"tool": name, "reason": "tool-call budget"})
                continue
            selected.append(
                {
                    "tool": name,
                    "arguments": {primary: state["question"][:200]},
                    "group": tool_group(name),
                }
            )

        if dropped:
            log.info("agent.tools_dropped", run_id=state.get("run_id"), dropped=dropped)

        return {
            "pending_tool_calls": selected,
            "steps": [
                _step(
                    "select_tools",
                    f"Authorised {len(selected)} of {len(plan.get('steps', []))} planned call(s).",
                    selected=[s["tool"] for s in selected],
                    dropped=dropped,
                )
            ],
        }

    # =================================================== 4. executors =======
    async def _execute_group(self, state: AgentState, group: str) -> dict[str, Any]:
        calls = [
            (c["tool"], c["arguments"])
            for c in state.get("pending_tool_calls") or []
            if c.get("group") == group
        ]
        if not calls:
            return {}

        if deadline_exceeded(state.get("deadline", 0.0)):
            return {
                "errors": [
                    {
                        "node": f"{group}_tools",
                        "code": "agent_budget_exceeded",
                        "message": "Runtime budget exhausted before execution",
                    }
                ],
                "steps": [_step(f"{group}_tools", "Skipped: runtime budget exhausted.")],
            }

        ctx = self._tool_context(state)
        started = time.perf_counter()
        results: list[ToolResult] = await self.deps.registry.call_many(calls, ctx)
        elapsed = int((time.perf_counter() - started) * 1000)

        evidence: list[dict[str, Any]] = []
        serialised: list[dict[str, Any]] = []
        cache_hits = 0
        for result in results:
            serialised.append(
                {
                    "tool": result.tool,
                    "ok": result.ok,
                    "summary": result.summary,
                    "count": result.count,
                    "latency_ms": result.latency_ms,
                    "degraded": result.degraded,
                    "cache_hit": result.cache_hit,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                    "payload": result.to_model_payload(),
                }
            )
            cache_hits += int(result.cache_hit)
            evidence.extend(item.model_dump(mode="json") for item in result.evidence)

        return {
            "tool_results": serialised,
            "evidence": evidence,
            "tool_calls_used": len(calls),
            "usage": {"tool_latency_ms": elapsed, "cache_hits": cache_hits},
            "steps": [
                _step(
                    f"{group}_tools",
                    f"Ran {len(calls)} {group} tool call(s); "
                    f"{sum(r.count for r in results)} record(s).",
                    tools=[r.tool for r in results],
                    ok=[r.ok for r in results],
                    duration_ms=elapsed,
                )
            ],
        }

    async def rag_tools(self, state: AgentState) -> dict[str, Any]:
        return await self._execute_group(state, "rag")

    async def sql_tools(self, state: AgentState) -> dict[str, Any]:
        return await self._execute_group(state, "sql")

    async def external_tools(self, state: AgentState) -> dict[str, Any]:
        return await self._execute_group(state, "external")

    async def sensitive_tools(self, state: AgentState) -> dict[str, Any]:
        return await self._execute_group(state, "sensitive")

    # =================================================== 5. analyze =========
    async def analyze(self, state: AgentState) -> dict[str, Any]:
        """Synthesise a draft answer strictly from the gathered evidence."""
        principal = principal_from_state(state)
        evidence = self._collected_evidence(state)
        block, valid_ids = self._evidence_block(evidence)

        degraded = [r for r in state.get("tool_results") or [] if r.get("degraded")]
        failed = [r for r in state.get("tool_results") or [] if not r.get("ok")]

        notes = []
        if degraded:
            notes.append(
                "NOTE: some results came from a stale local mirror because an "
                "upstream service was unavailable. Say so in your caveats."
            )
        if failed:
            notes.append(
                "NOTE: some tool calls failed: "
                + "; ".join(f"{r['tool']} ({r.get('error_code')})" for r in failed)
                + ". Do not speculate about what they would have returned."
            )

        user_content = "\n\n".join(
            [
                principal_context(principal),
                build_user_question_block(state["question"]),
                block,
                *notes,
                "Answer the question using only the evidence above. Cite source_id "
                "values verbatim in the citations field.",
            ]
        )
        messages = [
            Message(role="system", content=SYNTHESIS_SYSTEM),
            *[
                Message(role=turn["role"], content=turn["content"])  # type: ignore[arg-type]
                for turn in (state.get("history") or [])[-6:]
            ],
            Message(role="user", content=user_content),
        ]

        try:
            result = await self.deps.llm.structured_output(
                messages, ResearchAnswer, task="reasoning"
            )
        except (LLMError, LLMOutputValidationError) as exc:
            log.warning("agent.analyze_failed", error=exc.message, run_id=state.get("run_id"))
            return {
                "status": AgentRunStatus.FAILED.value,
                "errors": [{"node": "analyze", "code": exc.code, "message": exc.message}],
                "steps": [_step("analyze", f"Synthesis failed: {exc.message}")],
                "usage": empty_usage(),
            }

        answer: ResearchAnswer = result.parsed
        return {
            "draft_answer": answer.model_dump(mode="json"),
            "usage": _usage_delta(result.usage),
            "steps": [
                _step(
                    "analyze",
                    f"Drafted an answer citing {len(answer.citations)} source(s) "
                    f"from {len(valid_ids)} available.",
                    confidence=answer.confidence,
                    citations=answer.citations,
                )
            ],
        }

    # =================================================== 6. verify ==========
    async def verify(self, state: AgentState) -> dict[str, Any]:
        """Check the draft against the evidence.

        Two layers, deliberately:

        * a **deterministic** citation check - every cited id must be in the
          evidence set. This is set arithmetic; it cannot be talked out of a
          verdict and costs nothing.
        * an **LLM** grounding check for claims that are cited but not actually
          supported by what the citation says, which set arithmetic cannot see.

        The deterministic result wins where they disagree.
        """
        draft = state.get("draft_answer")
        if not draft:
            return {
                "verification": {
                    "grounded": False,
                    "sufficient": False,
                    "unsupported_claims": [],
                    "invalid_citations": [],
                    "missing_information": "No draft answer was produced.",
                    "recommendation": "refuse",
                    "reasoning": "Synthesis did not produce an answer.",
                },
                "steps": [_step("verify", "No draft to verify.")],
            }

        evidence = self._collected_evidence(state)
        block, valid_ids = self._evidence_block(evidence)
        cited = list(draft.get("citations") or [])
        invalid = sorted({c for c in cited if c not in valid_ids})

        user_content = "\n\n".join(
            [
                build_user_question_block(state["question"]),
                block,
                f"DRAFT ANSWER:\n{draft.get('answer', '')}",
                f"CITATIONS CLAIMED: {cited}",
                f"DETERMINISTIC CHECK: {len(invalid)} citation(s) do not resolve "
                f"to supplied evidence: {invalid}",
            ]
        )
        messages = [
            Message(role="system", content=VERIFIER_SYSTEM),
            Message(role="user", content=user_content),
        ]

        usage_delta = empty_usage()
        try:
            result = await self.deps.llm.structured_output(
                messages, VerificationResult, task="reasoning"
            )
            verification: VerificationResult = result.parsed
            usage_delta = _usage_delta(result.usage)
        except (LLMError, LLMOutputValidationError) as exc:
            # If the verifier is unavailable, fall back to the deterministic
            # check alone and downgrade rather than trusting the draft blindly.
            log.warning("agent.verify_failed", error=exc.message, run_id=state.get("run_id"))
            verification = VerificationResult(
                grounded=not invalid,
                sufficient=bool(evidence),
                unsupported_claims=[],
                invalid_citations=invalid,
                missing_information=None if evidence else "No evidence retrieved.",
                recommendation="answer_with_caveats" if evidence else "retrieve_more",
                reasoning="Verifier model unavailable; deterministic citation check only.",
            )

        # Deterministic result overrides the model's opinion.
        if invalid:
            verification.invalid_citations = sorted(
                set(verification.invalid_citations) | set(invalid)
            )
            verification.grounded = False
            if verification.recommendation == "accept":
                verification.recommendation = "refuse"

        if not evidence and verification.recommendation == "accept":
            verification.recommendation = "retrieve_more"
            verification.sufficient = False

        log.info(
            "agent.verified",
            run_id=state.get("run_id"),
            grounded=verification.grounded,
            recommendation=verification.recommendation,
            invalid_citations=len(verification.invalid_citations),
        )
        return {
            "verification": verification.model_dump(mode="json"),
            "usage": usage_delta,
            "steps": [
                _step(
                    "verify",
                    f"{verification.recommendation} "
                    f"(grounded={verification.grounded}, "
                    f"invalid citations={len(verification.invalid_citations)})",
                    reasoning=verification.reasoning,
                )
            ],
        }

    # =================================================== 7. retrieve_more ===
    async def retrieve_more(self, state: AgentState) -> dict[str, Any]:
        """Broaden retrieval for one more pass.

        Deliberately simple: drop the metadata filters and widen K. Re-running
        the identical query would return the identical chunks and burn an
        iteration for nothing.
        """
        verification = state.get("verification") or {}
        missing = verification.get("missing_information") or ""
        query = f"{state['question']} {missing}".strip()[:400]
        return {
            "iterations": state.get("iterations", 0) + 1,
            # Remembered so the verify router can detect a retry that added
            # nothing and stop, instead of burning the whole iteration budget
            # re-running a query that already came back empty.
            "evidence_before_retry": len(self._collected_evidence(state)),
            "pending_tool_calls": [
                {
                    "tool": "retrieve_documents",
                    "arguments": {"query": query, "limit": 12},
                    "group": "rag",
                }
            ],
            "steps": [
                _step(
                    "retrieve_more",
                    f"Iteration {state.get('iterations', 0) + 1}: widened retrieval.",
                    query=query,
                )
            ],
        }

    # =================================================== 8. approval ========
    async def approval_gate(self, state: AgentState) -> dict[str, Any]:
        """Pause the run and wait for a human decision.

        `interrupt()` checkpoints the entire state to Postgres and returns
        control to the caller. The run can be resumed minutes later, in a
        different process, with `Command(resume=...)`. That is precisely why
        the state had to stay serialisable.
        """
        from langgraph.types import interrupt

        # Read the call `select_tools` already authorised, not the raw plan:
        # the human is approving one specific, validated action.
        pending = [
            c
            for c in state.get("pending_tool_calls") or []
            if c.get("tool") == "request_sensitive_action"
        ]
        arguments = pending[0].get("arguments", {}) if pending else {}
        payload = {
            "action": arguments.get("action", "archive_document"),
            "target": arguments.get("target", "unspecified"),
            "justification": arguments.get("justification", ""),
            "requested_by": state.get("principal", {}).get("email"),
            "question": state["question"],
            "risk_level": "high",
        }

        log.info("agent.awaiting_approval", run_id=state.get("run_id"), action=payload["action"])

        # Execution stops here until someone resumes the thread.
        decision = interrupt(payload)

        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        note = decision.get("note") if isinstance(decision, dict) else None
        decided_by = decision.get("decided_by") if isinstance(decision, dict) else None

        if not approved:
            log.info("agent.approval_rejected", run_id=state.get("run_id"))
            return {
                "status": AgentRunStatus.REJECTED.value,
                "approval": {**payload, "approved": False, "note": note, "decided_by": decided_by},
                "final_answer": {
                    "answer": (
                        "This request was declined by a human reviewer, so no action was taken."
                        + (f" Reviewer note: {note}" if note else "")
                    ),
                    "citations": [],
                    "confidence": "high",
                    "caveats": ["Sensitive actions require human approval."],
                    "unanswered_aspects": [],
                },
                "steps": [_step("approval_gate", "Rejected by reviewer.", note=note)],
            }

        log.warning(
            "agent.approval_granted",
            run_id=state.get("run_id"),
            action=payload["action"],
            decided_by=decided_by,
        )
        return {
            "approved_actions": ["request_sensitive_action"],
            "approval": {**payload, "approved": True, "note": note, "decided_by": decided_by},
            "pending_tool_calls": [
                {
                    "tool": "request_sensitive_action",
                    "arguments": {
                        "action": payload["action"],
                        "target": payload["target"],
                        "justification": payload["justification"] or "Approved by reviewer.",
                    },
                    "group": "sensitive",
                }
            ],
            "steps": [
                _step("approval_gate", "Approved by reviewer.", decided_by=decided_by, note=note)
            ],
        }

    # =================================================== 9. direct answer ===
    async def direct_answer(self, state: AgentState) -> dict[str, Any]:
        """Answer without tools: greetings and capability questions."""
        messages = [
            Message(role="system", content=DIRECT_ANSWER_SYSTEM),
            Message(role="user", content=build_user_question_block(state["question"])),
        ]
        try:
            response = await self.deps.llm.chat(messages, task="fast")
            content = response.content
            usage = _usage_delta(response.usage)
        except LLMError as exc:
            content = "I could not reach the language model to answer that just now."
            usage = empty_usage()
            log.warning("agent.direct_answer_failed", error=exc.message)

        return {
            "final_answer": {
                "answer": content,
                "citations": [],
                "confidence": "high",
                "caveats": [],
                "unanswered_aspects": [],
            },
            "status": AgentRunStatus.COMPLETED.value,
            "usage": usage,
            "steps": [_step("direct_answer", "Answered without tools.")],
        }

    # =================================================== 10. finalize =======
    async def finalize(self, state: AgentState) -> dict[str, Any]:
        """Assemble the final answer, honouring the verifier's recommendation."""
        if state.get("final_answer"):
            return {"status": state.get("status") or AgentRunStatus.COMPLETED.value}

        # A run that performed an approved sensitive action reports that action,
        # not a literature synthesis.
        sensitive = [
            r
            for r in state.get("tool_results") or []
            if r.get("tool") == "request_sensitive_action"
        ]
        if sensitive and not state.get("draft_answer"):
            result = sensitive[0]
            ok = result.get("ok")
            return {
                "final_answer": {
                    "answer": (
                        f"{result.get('summary')}"
                        if ok
                        else f"The approved action could not be completed: "
                        f"{result.get('error_message')}"
                    ),
                    "citations": [],
                    "confidence": "high" if ok else "low",
                    "caveats": ["This action was executed only after explicit human approval."],
                    "unanswered_aspects": [],
                },
                "status": (AgentRunStatus.COMPLETED.value if ok else AgentRunStatus.FAILED.value),
                "steps": [_step("finalize", f"Sensitive action reported (ok={ok}).")],
            }

        draft = state.get("draft_answer") or {}
        verification = state.get("verification") or {}
        recommendation = verification.get("recommendation", "accept")
        evidence = self._collected_evidence(state)
        valid_ids = {item["source_id"] for item in evidence}

        caveats = list(draft.get("caveats") or [])
        # "running" is the in-flight marker, not an outcome: reaching finalize
        # means the run completed unless a node set a terminal failure status.
        current = state.get("status")
        status = (
            current
            if current
            and current not in (AgentRunStatus.RUNNING.value, AgentRunStatus.PENDING.value)
            else AgentRunStatus.COMPLETED.value
        )

        if recommendation == "refuse":
            answer = (
                "I could not produce an answer I can stand behind: the draft "
                "contained claims or citations that the retrieved evidence does "
                "not support. Rather than return something unverifiable, here is "
                "what was actually retrieved."
            )
            if evidence:
                answer += "\n\nRetrieved sources:\n" + "\n".join(
                    f"- [{item['source_id']}] {item.get('title')}" for item in evidence[:8]
                )
            citations = sorted(valid_ids)[:8]
            confidence = "low"
            caveats.append("The verification step rejected the drafted answer.")
        else:
            answer = draft.get("answer") or "No answer was produced."
            # Strip citations that did not survive verification.
            citations = [c for c in (draft.get("citations") or []) if c in valid_ids]
            confidence = draft.get("confidence", "low")
            if recommendation == "answer_with_caveats":
                confidence = "low" if confidence == "high" else confidence
                if verification.get("missing_information"):
                    caveats.append(f"Incomplete evidence: {verification['missing_information']}")

        if status == AgentRunStatus.BUDGET_EXCEEDED.value:
            caveats.append(
                "The run hit its execution budget, so this answer may be based on "
                "fewer sources than the question warrants."
            )
        degraded = [r for r in state.get("tool_results") or [] if r.get("degraded")]
        if degraded:
            caveats.append(
                "Some data came from a local mirror because an upstream service was "
                "unavailable and may be out of date."
            )

        log.info(
            "agent.finalized",
            run_id=state.get("run_id"),
            status=status,
            citations=len(citations),
            recommendation=recommendation,
        )
        return {
            "final_answer": {
                "answer": answer,
                "citations": citations,
                "confidence": confidence,
                "caveats": caveats,
                "unanswered_aspects": draft.get("unanswered_aspects") or [],
            },
            "status": status,
            "steps": [
                _step(
                    "finalize",
                    f"Final answer ready ({recommendation}, {len(citations)} citation(s)).",
                )
            ],
        }

    # =================================================== failure ============
    async def handle_failure(self, state: AgentState) -> dict[str, Any]:
        """Terminal node for unrecoverable states. Always returns something safe."""
        errors = state.get("errors") or []
        detail = errors[-1]["message"] if errors else "The run could not be completed."
        return {
            "status": AgentRunStatus.FAILED.value,
            "final_answer": {
                "answer": (
                    "I was not able to complete this request. "
                    f"Reason: {detail} Nothing was inferred or guessed in its place."
                ),
                "citations": [],
                "confidence": "low",
                "caveats": ["The run ended in a failure state."],
                "unanswered_aspects": [state.get("question", "")[:200]],
            },
            "steps": [_step("handle_failure", detail)],
        }


def evidence_items(state: AgentState) -> list[EvidenceItem]:
    """Typed evidence for the API response layer."""
    out: list[EvidenceItem] = []
    seen: set[str] = set()
    for raw in state.get("evidence") or []:
        sid = raw.get("source_id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        try:
            out.append(EvidenceItem.model_validate(raw))
        except (ValueError, TypeError) as exc:
            log.debug("agent.evidence_unparsable", error=str(exc))
    return out


__all__ = ["AgentDependencies", "AgentNodes", "AppError", "evidence_items", "tool_group"]
