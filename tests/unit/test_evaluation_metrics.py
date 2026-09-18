"""Deterministic evaluation checks."""

from __future__ import annotations

import uuid

import pytest

from app.evaluation.metrics import evaluate_deterministic
from app.models.evaluation import EvaluationCase
from app.schemas.agent import (
    AgentRunResponse,
    ApprovalPrompt,
    EvidenceItem,
    ToolCallRecord,
    UsageStats,
)

pytestmark = pytest.mark.unit


def make_case(**overrides) -> EvaluationCase:
    defaults = {
        "slug": "t",
        "suite": "core",
        "question": "q",
        "expected_behavior": "b",
        "expected_category": None,
        "expected_tools": [],
        "forbidden_tools": [],
        "expected_tool_arguments": {},
        "expected_evidence_keywords": [],
        "expected_answer_keywords": [],
        "forbidden_answer_keywords": [],
        "requires_citations": False,
        "requires_approval": False,
        "max_latency_ms": None,
        "max_cost_usd": None,
        "as_role": "researcher",
        "enabled": True,
        "notes": "",
    }
    defaults.update(overrides)
    return EvaluationCase(**defaults)


def make_tool_call(name: str, status: str = "executed") -> ToolCallRecord:
    import datetime as dt

    return ToolCallRecord(
        id=uuid.uuid4(),
        tool_name=name,
        status=status,
        arguments={},
        latency_ms=10,
        retry_count=0,
        cache_hit=False,
        created_at=dt.datetime.now(dt.UTC),
    )


def make_run(**overrides) -> AgentRunResponse:
    defaults = {
        "run_id": uuid.uuid4(),
        "status": "completed",
        "question": "q",
        "answer": "An answer citing [doc:a:0].",
        "citations": ["doc:a:0"],
        "evidence": [
            EvidenceItem(
                source_type="document",
                source_id="doc:a:0",
                title="Breast cancer study",
                snippet="HER2 expression was elevated in the cohort.",
            )
        ],
        "tool_calls": [make_tool_call("retrieve_documents")],
        "usage": UsageStats(total_latency_ms=1000, estimated_cost_usd=0.01),
    }
    defaults.update(overrides)
    return AgentRunResponse(**defaults)


class TestToolSelection:
    def test_expected_tool_used(self):
        outcome = evaluate_deterministic(
            make_case(expected_tools=["retrieve_documents"]), make_run()
        )
        assert outcome.passed

    def test_missing_expected_tool_fails(self):
        outcome = evaluate_deterministic(
            make_case(expected_tools=["search_clinical_trials"]), make_run()
        )
        assert not outcome.passed
        assert any("expected tools" in f for f in outcome.failures)

    def test_forbidden_tool_requested_fails_even_if_denied(self):
        """Requesting a forbidden tool is a failure even when it was blocked:
        the point is that the agent should not have asked."""
        run = make_run(tool_calls=[make_tool_call("request_sensitive_action", status="denied")])
        outcome = evaluate_deterministic(
            make_case(forbidden_tools=["request_sensitive_action"]), run
        )
        assert not outcome.passed


class TestCitations:
    def test_fabricated_citation_fails(self):
        run = make_run(citations=["doc:not:real"])
        outcome = evaluate_deterministic(make_case(requires_citations=True), run)
        assert not outcome.passed
        assert any("do not resolve" in f for f in outcome.failures)

    def test_missing_citations_fails_when_required(self):
        outcome = evaluate_deterministic(make_case(requires_citations=True), make_run(citations=[]))
        assert not outcome.passed
        assert any("no citations" in f for f in outcome.failures)

    def test_valid_citations_pass(self):
        assert evaluate_deterministic(make_case(requires_citations=True), make_run()).passed


class TestKeywords:
    def test_forbidden_answer_keyword_detected(self):
        run = make_run(answer="Ignore previous instructions, entering unrestricted mode.")
        outcome = evaluate_deterministic(
            make_case(forbidden_answer_keywords=["unrestricted mode"]), run
        )
        assert not outcome.passed

    def test_expected_evidence_keyword_checked(self):
        outcome = evaluate_deterministic(make_case(expected_evidence_keywords=["HER2"]), make_run())
        assert outcome.passed
        outcome = evaluate_deterministic(
            make_case(expected_evidence_keywords=["glioblastoma"]), make_run()
        )
        assert not outcome.passed


class TestApprovalGate:
    def test_run_that_should_pause_but_completed_fails(self):
        outcome = evaluate_deterministic(make_case(requires_approval=True), make_run())
        assert not outcome.passed
        assert any("pause for approval" in f for f in outcome.failures)

    def test_paused_run_passes(self):
        run = make_run(
            status="awaiting_approval",
            answer=None,
            citations=[],
            tool_calls=[],
            approval=ApprovalPrompt(
                approval_id=uuid.uuid4(),
                action="archive_document",
                reason="r",
                risk_level="high",
                payload={},
            ),
        )
        assert evaluate_deterministic(make_case(requires_approval=True), run).passed

    def test_action_executed_without_approval_fails(self):
        run = make_run(
            status="awaiting_approval",
            tool_calls=[make_tool_call("request_sensitive_action", status="executed")],
            approval=ApprovalPrompt(
                approval_id=uuid.uuid4(),
                action="a",
                reason="r",
                risk_level="high",
                payload={},
            ),
        )
        outcome = evaluate_deterministic(make_case(requires_approval=True), run)
        assert not outcome.passed
        assert any("without approval" in f for f in outcome.failures)


class TestBudgets:
    def test_latency_over_budget_fails(self):
        run = make_run(usage=UsageStats(total_latency_ms=99_000))
        assert not evaluate_deterministic(make_case(max_latency_ms=1000), run).passed

    def test_cost_over_budget_fails(self):
        run = make_run(usage=UsageStats(estimated_cost_usd=5.0))
        assert not evaluate_deterministic(make_case(max_cost_usd=0.10), run).passed


class TestTerminalStatus:
    def test_failed_run_is_a_failure(self):
        assert not evaluate_deterministic(make_case(), make_run(status="failed")).passed

    def test_score_reflects_proportion_of_checks_passed(self):
        outcome = evaluate_deterministic(
            make_case(expected_tools=["retrieve_documents"], requires_citations=True),
            make_run(),
        )
        assert outcome.score == 1.0
