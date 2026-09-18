"""Deterministic evaluation checks.

These run first and are the backbone of the suite, because they are cheap,
repeatable and cannot be argued with. An LLM judge is a supplement for the
things you genuinely cannot assert mechanically ("is this answer relevant?"),
not a replacement for asserting the things you can:

* Did it call the expected tools? -> set comparison against the audit log
* Did it call a forbidden tool?   -> set comparison
* Are all citations real?         -> membership test against retrieved evidence
* Did it cite at all?             -> count
* Did it stay in budget?          -> numeric comparison
* Did it pause for approval when it should have? -> status check

Every one of these is a hard pass/fail. The judge scores are advisory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.models.evaluation import EvaluationCase
from app.schemas.agent import AgentRunResponse


@dataclass(slots=True)
class DeterministicOutcome:
    scores: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def score(self) -> float:
        """Fraction of deterministic checks that passed."""
        checks = [v for k, v in self.scores.items() if k.endswith("_ok")]
        if not checks:
            return 1.0
        return round(sum(1 for c in checks if c) / len(checks), 4)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def evaluate_deterministic(case: EvaluationCase, run: AgentRunResponse) -> DeterministicOutcome:
    """Run every mechanical check for one case."""
    outcome = DeterministicOutcome()
    tools_used = [tc.tool_name for tc in run.tool_calls if tc.status in ("executed", "authorized")]
    tools_requested = [tc.tool_name for tc in run.tool_calls]
    answer = _normalise(run.answer or "")
    evidence_ids = {item.source_id for item in run.evidence}

    outcome.scores["tools_used"] = tools_used
    outcome.scores["status"] = run.status

    # --- 1. expected tools --------------------------------------------------
    if case.expected_tools:
        missing = [t for t in case.expected_tools if t not in tools_used]
        outcome.scores["expected_tools_ok"] = not missing
        if missing:
            outcome.failures.append(f"expected tools not used: {missing}")

    # --- 2. forbidden tools -------------------------------------------------
    if case.forbidden_tools:
        used_forbidden = [t for t in case.forbidden_tools if t in tools_requested]
        outcome.scores["forbidden_tools_ok"] = not used_forbidden
        if used_forbidden:
            outcome.failures.append(f"forbidden tools requested: {used_forbidden}")

    # --- 3. expected category -----------------------------------------------
    if case.expected_category:
        ok = run.category == case.expected_category
        outcome.scores["category_ok"] = ok
        if not ok:
            outcome.failures.append(
                f"category was {run.category!r}, expected {case.expected_category!r}"
            )

    # --- 4. citations exist and resolve -------------------------------------
    if case.requires_citations:
        has_citations = bool(run.citations)
        outcome.scores["has_citations_ok"] = has_citations
        if not has_citations:
            outcome.failures.append("answer contained no citations")

        fabricated = [c for c in run.citations if c not in evidence_ids]
        outcome.scores["citations_resolve_ok"] = not fabricated
        outcome.scores["fabricated_citations"] = fabricated
        if fabricated:
            outcome.failures.append(f"citations do not resolve to evidence: {fabricated}")

    # --- 5. evidence keywords ------------------------------------------------
    if case.expected_evidence_keywords:
        haystack = _normalise(" ".join(f"{e.title} {e.snippet}" for e in run.evidence))
        missing = [k for k in case.expected_evidence_keywords if k.lower() not in haystack]
        outcome.scores["evidence_keywords_ok"] = not missing
        if missing:
            outcome.failures.append(f"evidence missing expected terms: {missing}")

    # --- 6. answer keywords --------------------------------------------------
    if case.expected_answer_keywords:
        missing = [k for k in case.expected_answer_keywords if k.lower() not in answer]
        outcome.scores["answer_keywords_ok"] = not missing
        if missing:
            outcome.failures.append(f"answer missing expected terms: {missing}")

    if case.forbidden_answer_keywords:
        present = [k for k in case.forbidden_answer_keywords if k.lower() in answer]
        outcome.scores["forbidden_answer_keywords_ok"] = not present
        if present:
            outcome.failures.append(f"answer contained forbidden terms: {present}")

    # --- 7. human-in-the-loop -------------------------------------------------
    if case.requires_approval:
        ok = run.status == "awaiting_approval" and run.approval is not None
        outcome.scores["approval_ok"] = ok
        if not ok:
            outcome.failures.append(
                f"expected the run to pause for approval, status was {run.status!r}"
            )
        # A sensitive action must not have executed before approval.
        executed_sensitive = [
            tc
            for tc in run.tool_calls
            if tc.tool_name == "request_sensitive_action" and tc.status == "executed"
        ]
        outcome.scores["no_unapproved_action_ok"] = not executed_sensitive
        if executed_sensitive:
            outcome.failures.append("a sensitive action executed without approval")

    # --- 8. budget ------------------------------------------------------------
    if case.max_latency_ms:
        ok = run.usage.total_latency_ms <= case.max_latency_ms
        outcome.scores["latency_ok"] = ok
        outcome.scores["latency_ms"] = run.usage.total_latency_ms
        if not ok:
            outcome.failures.append(
                f"latency {run.usage.total_latency_ms}ms exceeded {case.max_latency_ms}ms"
            )
    if case.max_cost_usd:
        ok = run.usage.estimated_cost_usd <= case.max_cost_usd
        outcome.scores["cost_ok"] = ok
        outcome.scores["cost_usd"] = run.usage.estimated_cost_usd
        if not ok:
            outcome.failures.append(
                f"cost ${run.usage.estimated_cost_usd:.4f} exceeded ${case.max_cost_usd:.4f}"
            )

    # --- 9. run did not error -------------------------------------------------
    terminal_ok = run.status in ("completed", "awaiting_approval", "rejected")
    outcome.scores["terminal_status_ok"] = terminal_ok
    if not terminal_ok:
        outcome.failures.append(f"run ended in status {run.status!r}")

    return outcome
