"""Evaluation runner.

Executes every case in a suite as a real agent run — same graph, same tools,
same authorisation — then scores it. Each case runs as the role the case
declares (`as_role`), because "does a researcher get blocked from restricted
material" is itself something the suite must verify, not an assumption.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select

from app.auth.permissions import ROLE_PERMISSIONS, Principal
from app.core.logging import LogContext, get_logger
from app.database.session import session_scope
from app.evaluation.dataset import get_cases
from app.evaluation.judge import JUDGE_PASS_THRESHOLD, LLMJudge
from app.evaluation.metrics import evaluate_deterministic
from app.llm.base import LLMClient
from app.llm.factory import get_llm_client
from app.models.evaluation import EvaluationCase, EvaluationResult
from app.models.user import User
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.schemas.evaluation import SuiteSummary
from app.services.agent_service import AgentService

log = get_logger(__name__)


class EvaluationRunner:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or get_llm_client()

    # ------------------------------------------------------------------ main
    async def run_suite(
        self,
        *,
        suite: str = "core",
        run_label: str | None = None,
        use_judge: bool = True,
        case_slugs: list[str] | None = None,
    ) -> SuiteSummary:
        label = run_label or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        async with session_scope() as session:
            cases = await get_cases(session, suite=suite, slugs=case_slugs)
            principals = await self._principals_by_role(session)

        if not cases:
            log.warning("evaluation.no_cases", suite=suite)
            return SuiteSummary(
                run_label=label,
                total=0,
                passed=0,
                failed=0,
                pass_rate=0.0,
                mean_score=0.0,
                mean_latency_ms=0.0,
                total_cost_usd=0.0,
            )

        log.info("evaluation.suite_started", suite=suite, cases=len(cases), label=label)

        scores: list[float] = []
        latencies: list[int] = []
        total_cost = 0.0
        passed = 0
        by_failure: dict[str, int] = {}

        for case in cases:
            principal = principals.get(case.as_role)
            if principal is None:
                log.warning("evaluation.no_principal", case=case.slug, role=case.as_role)
                continue

            with LogContext(eval_case=case.slug, eval_label=label):
                result = await self._run_case(case, principal, label, use_judge)

            scores.append(result["overall_score"])
            latencies.append(result["latency_ms"])
            total_cost += result["cost_usd"]
            if result["passed"]:
                passed += 1
            for failure in result["failures"]:
                key = failure.split(":")[0][:60]
                by_failure[key] = by_failure.get(key, 0) + 1

        total = len(scores)
        summary = SuiteSummary(
            run_label=label,
            total=total,
            passed=passed,
            failed=total - passed,
            pass_rate=round(passed / total, 4) if total else 0.0,
            mean_score=round(sum(scores) / total, 4) if total else 0.0,
            mean_latency_ms=round(sum(latencies) / total, 2) if total else 0.0,
            total_cost_usd=round(total_cost, 6),
            by_failure=by_failure,
        )
        log.info(
            "evaluation.suite_finished",
            suite=suite,
            label=label,
            passed=passed,
            total=total,
            pass_rate=summary.pass_rate,
            cost_usd=summary.total_cost_usd,
        )
        return summary

    # -------------------------------------------------------------- one case
    async def _run_case(
        self,
        case: EvaluationCase,
        principal: Principal,
        label: str,
        use_judge: bool,
    ) -> dict[str, Any]:
        async with session_scope() as session:
            service = AgentService(session)
            try:
                run: AgentRunResponse = await service.run(
                    AgentRunRequest(question=case.question), principal
                )
            except Exception as exc:  # noqa: BLE001 - a crash is a failed case
                log.error("evaluation.case_crashed", case=case.slug, error=str(exc)[:300])
                session.add(
                    EvaluationResult(
                        case_id=case.id,
                        run_label=label,
                        passed=False,
                        failures=[f"agent run raised: {type(exc).__name__}: {exc}"[:400]],
                        overall_score=0.0,
                    )
                )
                return {
                    "passed": False,
                    "overall_score": 0.0,
                    "latency_ms": 0,
                    "cost_usd": 0.0,
                    "failures": ["agent run raised"],
                }

        deterministic = evaluate_deterministic(case, run)
        failures = list(deterministic.failures)

        judge_scores: dict[str, Any] = {}
        judge_reasoning: str | None = None
        judge_cost = 0.0
        if use_judge:
            verdict, usage = await LLMJudge(self._llm).grade(case, run)
            judge_cost = usage.cost_usd
            if verdict is not None:
                judge_scores = verdict.as_dict()
                judge_reasoning = verdict.reasoning
                if verdict.mean < JUDGE_PASS_THRESHOLD:
                    failures.append(
                        f"judge mean {verdict.mean:.2f} below threshold {JUDGE_PASS_THRESHOLD}"
                    )

        # Deterministic checks carry most of the weight; the judge is advisory.
        judge_mean = float(judge_scores.get("mean", 0.0)) if judge_scores else None
        overall = (
            round(0.7 * deterministic.score + 0.3 * judge_mean, 4)
            if judge_mean is not None
            else deterministic.score
        )
        passed = not failures

        async with session_scope() as session:
            session.add(
                EvaluationResult(
                    case_id=case.id,
                    agent_run_id=run.run_id,
                    run_label=label,
                    passed=passed,
                    deterministic_scores=deterministic.scores,
                    judge_scores=judge_scores,
                    judge_reasoning=judge_reasoning,
                    overall_score=overall,
                    failures=failures,
                    latency_ms=run.usage.total_latency_ms,
                    cost_usd=round(run.usage.estimated_cost_usd + judge_cost, 6),
                    tools_used=[tc.tool_name for tc in run.tool_calls],
                    answer=(run.answer or "")[:20000],
                )
            )

        log.info(
            "evaluation.case_finished",
            case=case.slug,
            passed=passed,
            score=overall,
            failures=len(failures),
        )
        return {
            "passed": passed,
            "overall_score": overall,
            "latency_ms": run.usage.total_latency_ms,
            "cost_usd": run.usage.estimated_cost_usd + judge_cost,
            "failures": failures,
        }

    # ------------------------------------------------------------- principals
    @staticmethod
    async def _principals_by_role(session: Any) -> dict[str, Principal]:
        """One seeded user per role, so cases can run at different clearances."""
        users = (
            (await session.execute(select(User).where(User.is_active.is_(True))))
            .unique()
            .scalars()
            .all()
        )
        out: dict[str, Principal] = {}
        for user in users:
            for role in user.role_names:
                if role not in out:
                    out[role] = Principal(
                        user_id=str(user.id),
                        email=user.email,
                        full_name=user.full_name,
                        roles=frozenset(user.role_names),
                        permissions=frozenset(user.permissions),
                        department=user.department,
                    )
        # Safety net if a role has no seeded user.
        for role, perms in ROLE_PERMISSIONS.items():
            out.setdefault(
                role,
                Principal(
                    user_id=str(uuid.UUID(int=0)),
                    email=f"{role}@synthetic",
                    full_name=f"synthetic {role}",
                    roles=frozenset({role}),
                    permissions=frozenset(perms),
                ),
            )
        return out
