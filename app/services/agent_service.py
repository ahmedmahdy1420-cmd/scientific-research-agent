"""Agent orchestration service.

Sits between the API and the graph: owns the `agent_runs` lifecycle, the
approval records, and the translation from raw graph state into the typed
`AgentRunResponse` the API and frontend consume.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runner import AgentInvocation, AgentRunner, new_thread_id
from app.auth.permissions import Permission, Principal, owns_or_can_read_any
from app.core.errors import (
    AuthorizationError,
    NotFoundError,
    ResourceAccessDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.agent import AgentMessage, AgentRun, ApprovalRequest, ToolAuditLog
from app.models.enums import AgentRunStatus, ApprovalStatus, MessageRole
from app.schemas.agent import (
    AgentPlan,
    AgentRunRequest,
    AgentRunResponse,
    AgentStep,
    ApprovalPrompt,
    EvidenceItem,
    ToolCallRecord,
    UsageStats,
    VerificationResult,
)

log = get_logger(__name__)

APPROVAL_TTL_MINUTES = 60


class AgentService:
    def __init__(self, session: AsyncSession, runner: AgentRunner | None = None) -> None:
        self._session = session
        self._runner = runner or AgentRunner()

    # =========================================================== run ========
    async def run(self, request: AgentRunRequest, principal: Principal) -> AgentRunResponse:
        """Create an agent run, execute the graph, persist the outcome."""
        thread_id = new_thread_id()
        run = AgentRun(
            user_id=uuid.UUID(principal.user_id),
            question=request.question,
            thread_id=thread_id,
            status=AgentRunStatus.RUNNING,
            started_at=dt.datetime.now(dt.UTC),
        )
        self._session.add(run)
        await self._session.flush()  # assigns run.id
        await self._add_message(run.id, 0, MessageRole.USER, request.question, node="input")
        # Commit before invoking the graph. The tool audit log writes in its own
        # transaction (so it survives a failed run), which means the run row must
        # already be visible to it -- and a crash mid-run must leave a record.
        await self._session.commit()

        invocation = await self._runner.start(
            run_id=run.id,
            thread_id=thread_id,
            question=request.question,
            principal=principal,
            history=[turn.model_dump() for turn in request.history],
            filters={
                "research_area": request.research_area,
                "date_from": request.date_from.isoformat() if request.date_from else None,
                "date_to": request.date_to.isoformat() if request.date_to else None,
            },
            force_tools=list(request.force_tools or []),
            max_iterations=request.max_iterations,
            max_tool_calls=request.max_tool_calls,
        )

        return await self._persist_and_respond(run, invocation, principal)

    # ====================================================== approvals =======
    async def decide_approval(
        self,
        run_id: uuid.UUID,
        *,
        approved: bool,
        note: str | None,
        principal: Principal,
    ) -> AgentRunResponse:
        """Approve or reject a paused run, then resume the graph.

        Authorisation is checked here, in application code, against the live
        database — not against anything the model produced.
        """
        if not principal.has_permission(Permission.APPROVALS_DECIDE):
            raise AuthorizationError(
                "You are not permitted to decide approvals",
                details={"required": Permission.APPROVALS_DECIDE},
            )

        run = await self._get_run(run_id)

        # Segregation of duties: you cannot approve your own request. An agent
        # that lets the requester rubber-stamp itself has a human in the loop
        # in name only.
        if str(run.user_id) == principal.user_id:
            log.warning(
                "approval.self_approval_blocked",
                run_id=str(run.id),
                user_id=principal.user_id,
            )
            raise AuthorizationError(
                "You cannot decide an approval for a run you started; "
                "another reviewer must approve it",
                details={"control": "segregation_of_duties"},
            )

        if run.status != AgentRunStatus.AWAITING_APPROVAL:
            raise ValidationError(
                f"Run is {run.status}, not awaiting approval",
                details={"status": run.status.value},
            )

        approval = (
            await self._session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.run_id == run.id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                )
                .order_by(desc(ApprovalRequest.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        if approval is None:
            raise NotFoundError("No pending approval for this run")

        now = dt.datetime.now(dt.UTC)
        if approval.expires_at and approval.expires_at < now:
            approval.status = ApprovalStatus.EXPIRED
            run.status = AgentRunStatus.FAILED
            run.error_code = "approval_expired"
            await self._session.flush()
            raise ValidationError("This approval request has expired")

        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.decided_by_id = uuid.UUID(principal.user_id)
        approval.decided_at = now
        approval.decision_note = note
        await self._session.flush()

        log.warning(
            "approval.decided",
            run_id=str(run.id),
            approved=approved,
            decided_by=principal.user_id,
            action=approval.action,
        )

        invocation = await self._runner.resume(
            run_id=run.id,
            thread_id=run.thread_id,
            approved=approved,
            decided_by=principal.email,
            note=note,
        )
        return await self._persist_and_respond(run, invocation, principal)

    # ========================================================== reads =======
    async def get_run_response(self, run_id: uuid.UUID, principal: Principal) -> AgentRunResponse:
        run = await self._get_run(run_id)
        if not owns_or_can_read_any(principal, str(run.user_id)):
            raise ResourceAccessDeniedError("You cannot view this agent run")

        tool_calls = (
            (
                await self._session.execute(
                    select(ToolAuditLog)
                    .where(ToolAuditLog.run_id == run.id)
                    .order_by(ToolAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        messages = (
            (
                await self._session.execute(
                    select(AgentMessage)
                    .where(AgentMessage.run_id == run.id)
                    .order_by(AgentMessage.sequence)
                )
            )
            .scalars()
            .all()
        )
        steps = [
            AgentStep(
                node=m.node or m.role.value,
                sequence=m.sequence,
                summary=m.content,
                detail=m.message_metadata or {},
            )
            for m in messages
            if m.role == MessageRole.ASSISTANT
        ]

        approval_prompt: ApprovalPrompt | None = None
        if run.status == AgentRunStatus.AWAITING_APPROVAL:
            pending = (
                await self._session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.run_id == run.id,
                        ApprovalRequest.status == ApprovalStatus.PENDING,
                    )
                    .order_by(desc(ApprovalRequest.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pending:
                approval_prompt = ApprovalPrompt(
                    approval_id=pending.id,
                    action=pending.action,
                    reason=pending.reason,
                    risk_level=pending.risk_level,
                    payload=pending.payload,
                    expires_at=pending.expires_at,
                )

        return AgentRunResponse(
            run_id=run.id,
            status=run.status.value,
            question=run.question,
            answer=run.final_answer,
            category=run.category,
            evidence=[EvidenceItem.model_validate(e) for e in run.evidence or []],
            citations=list(run.answer_citations or []),
            caveats=list(run.answer_caveats or []),
            confidence=run.answer_confidence,
            plan=self._safe_plan(run.plan),
            steps=steps,
            tool_calls=[ToolCallRecord.model_validate(t) for t in tool_calls],
            verification=(
                VerificationResult.model_validate(run.verification) if run.verification else None
            ),
            approval=approval_prompt,
            usage=UsageStats(
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                total_tokens=run.prompt_tokens + run.completion_tokens,
                estimated_cost_usd=run.estimated_cost_usd,
                llm_latency_ms=run.llm_latency_ms,
                tool_latency_ms=run.tool_latency_ms,
                total_latency_ms=run.total_latency_ms,
                iterations=run.iterations,
                tool_calls=run.tool_calls_used,
                models_used=run.models_used or [],
            ),
            error=run.error_message,
            created_at=run.created_at,
        )

    async def list_runs(
        self, principal: Principal, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[AgentRun], int]:
        stmt = select(AgentRun)
        count_stmt = select(func.count()).select_from(AgentRun)
        if not principal.has_permission(Permission.AGENT_READ_ANY):
            uid = uuid.UUID(principal.user_id)
            stmt = stmt.where(AgentRun.user_id == uid)
            count_stmt = count_stmt.where(AgentRun.user_id == uid)

        total = int((await self._session.execute(count_stmt)).scalar() or 0)
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(desc(AgentRun.created_at)).limit(limit).offset(offset)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        return list(rows), total

    # ======================================================== internals =====
    async def _get_run(self, run_id: uuid.UUID) -> AgentRun:
        run = (
            (await self._session.execute(select(AgentRun).where(AgentRun.id == run_id)))
            .unique()
            .scalar_one_or_none()
        )
        if run is None:
            raise NotFoundError(f"No agent run with id {run_id}")
        return run

    @staticmethod
    def _safe_plan(plan: dict[str, Any] | None) -> AgentPlan | None:
        if not plan:
            return None
        try:
            return AgentPlan.model_validate(plan)
        except (ValueError, TypeError):
            return None

    async def _add_message(
        self,
        run_id: uuid.UUID,
        sequence: int,
        role: MessageRole,
        content: str,
        *,
        node: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._session.add(
            AgentMessage(
                run_id=run_id,
                sequence=sequence,
                role=role,
                node=node,
                content=content[:20000],
                message_metadata=metadata or {},
            )
        )

    async def _persist_and_respond(
        self, run: AgentRun, invocation: AgentInvocation, principal: Principal
    ) -> AgentRunResponse:
        """Write graph output back to `agent_runs` and build the response."""
        state = invocation.state
        usage = state.get("usage") or {}
        final = state.get("final_answer") or {}
        verification_raw = state.get("verification")

        run.category = state.get("category")
        run.plan = state.get("plan")
        run.evidence = self._dedupe_evidence(state.get("evidence") or [])
        run.verification = verification_raw
        run.iterations = int(state.get("iterations", 0) or 0)
        run.tool_calls_used = int(state.get("tool_calls_used", 0) or 0)
        run.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        run.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        run.estimated_cost_usd = float(usage.get("estimated_cost_usd", 0.0) or 0.0)
        run.llm_latency_ms = int(usage.get("llm_latency_ms", 0) or 0)
        run.tool_latency_ms = int(usage.get("tool_latency_ms", 0) or 0)
        run.total_latency_ms = invocation.elapsed_ms
        run.models_used = list(usage.get("models_used") or [])

        # Persist the node timeline as assistant messages so a run can be
        # replayed in the UI without reading LangGraph's internal checkpoints.
        #
        # On a resume after human approval the checkpointed state carries the
        # steps from BEFORE the pause as well as the new ones, so only the tail
        # is appended -- re-inserting the whole timeline would collide with the
        # unique (run_id, sequence) index.
        steps = state.get("steps") or []
        already_persisted = int(
            await self._session.scalar(
                select(func.count())
                .select_from(AgentMessage)
                .where(
                    AgentMessage.run_id == run.id,
                    AgentMessage.role == MessageRole.ASSISTANT,
                )
            )
            or 0
        )
        next_sequence = (
            int(
                await self._session.scalar(
                    select(func.max(AgentMessage.sequence)).where(AgentMessage.run_id == run.id)
                )
                or 0
            )
            + 1
        )
        for offset, step in enumerate(steps[already_persisted:]):
            await self._add_message(
                run.id,
                next_sequence + offset,
                MessageRole.ASSISTANT,
                step.get("summary", ""),
                node=step.get("node"),
                metadata=step.get("detail") or {},
            )

        approval_prompt: ApprovalPrompt | None = None

        if invocation.interrupted:
            run.status = AgentRunStatus.AWAITING_APPROVAL
            payload = invocation.interrupt_payload or {}
            approval = ApprovalRequest(
                run_id=run.id,
                requested_by_id=uuid.UUID(principal.user_id),
                action=str(payload.get("action", "unspecified"))[:128],
                reason=str(payload.get("justification") or payload.get("question") or "")[:4000],
                payload=payload,
                risk_level=str(payload.get("risk_level", "high")),
                status=ApprovalStatus.PENDING,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=APPROVAL_TTL_MINUTES),
            )
            self._session.add(approval)
            await self._session.flush()
            approval_prompt = ApprovalPrompt(
                approval_id=approval.id,
                action=approval.action,
                reason=approval.reason,
                risk_level=approval.risk_level,
                payload=approval.payload,
                expires_at=approval.expires_at,
            )
        else:
            status_value = state.get("status") or AgentRunStatus.COMPLETED.value
            run.status = AgentRunStatus(status_value)
            run.final_answer = final.get("answer")
            run.answer_confidence = final.get("confidence")
            run.answer_citations = list(final.get("citations") or [])
            run.answer_caveats = list(final.get("caveats") or [])
            run.finished_at = dt.datetime.now(dt.UTC)
            errors = state.get("errors") or []
            if errors:
                run.error_code = errors[-1].get("code")
                run.error_message = errors[-1].get("message")

        await self._session.flush()

        log.info(
            "agent.run_persisted",
            run_id=str(run.id),
            status=run.status.value,
            latency_ms=run.total_latency_ms,
            cost_usd=run.estimated_cost_usd,
            tool_calls=run.tool_calls_used,
            iterations=run.iterations,
        )

        tool_calls = (
            (
                await self._session.execute(
                    select(ToolAuditLog)
                    .where(ToolAuditLog.run_id == run.id)
                    .order_by(ToolAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )

        return AgentRunResponse(
            run_id=run.id,
            status=run.status.value,
            question=run.question,
            answer=final.get("answer"),
            confidence=final.get("confidence"),
            category=run.category,
            evidence=[EvidenceItem.model_validate(e) for e in run.evidence or []],
            citations=list(final.get("citations") or []),
            caveats=list(final.get("caveats") or []),
            plan=self._safe_plan(run.plan),
            steps=[
                AgentStep(
                    node=step.get("node", "?"),
                    sequence=index,
                    summary=step.get("summary", ""),
                    detail=step.get("detail") or {},
                )
                for index, step in enumerate(state.get("steps") or [], start=1)
            ],
            tool_calls=[ToolCallRecord.model_validate(t) for t in tool_calls],
            verification=(
                VerificationResult.model_validate(verification_raw) if verification_raw else None
            ),
            approval=approval_prompt,
            usage=UsageStats(
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                total_tokens=run.prompt_tokens + run.completion_tokens,
                estimated_cost_usd=run.estimated_cost_usd,
                llm_latency_ms=run.llm_latency_ms,
                tool_latency_ms=run.tool_latency_ms,
                retrieval_latency_ms=int(usage.get("retrieval_latency_ms", 0) or 0),
                total_latency_ms=run.total_latency_ms,
                iterations=run.iterations,
                tool_calls=run.tool_calls_used,
                models_used=run.models_used or [],
                cache_hits=int(usage.get("cache_hits", 0) or 0),
            ),
            error=run.error_message,
            created_at=run.created_at,
        )

    @staticmethod
    def _dedupe_evidence(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in raw:
            sid = item.get("source_id")
            if sid and sid not in seen:
                seen.add(sid)
                out.append(item)
        return out
