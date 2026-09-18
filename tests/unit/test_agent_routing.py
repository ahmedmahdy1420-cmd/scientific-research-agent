"""Graph routing decisions.

Routers are pure functions of state, which makes the agent's control flow
unit-testable without running a model or touching a database.
"""

from __future__ import annotations

import pytest

from app.agents.graph import (
    route_after_analyze,
    route_after_approval,
    route_after_classify,
    route_after_verify,
    route_tools,
)
from app.agents.nodes import tool_group
from app.models.enums import AgentRunStatus, QuestionCategory

pytestmark = pytest.mark.unit


class TestClassifyRouting:
    def test_small_talk_skips_tools(self):
        assert route_after_classify({"category": QuestionCategory.SMALL_TALK.value}) == (
            "direct_answer"
        )

    def test_out_of_scope_skips_tools(self):
        assert route_after_classify({"category": QuestionCategory.OUT_OF_SCOPE.value}) == (
            "direct_answer"
        )

    @pytest.mark.parametrize(
        "category",
        [
            QuestionCategory.LITERATURE.value,
            QuestionCategory.CLINICAL_TRIALS.value,
            QuestionCategory.STRUCTURED_DATA.value,
            QuestionCategory.COMPARISON.value,
            QuestionCategory.SENSITIVE_ACTION.value,
        ],
    )
    def test_everything_else_plans_first(self, category):
        """Sensitive requests plan too: the planner picks WHICH action."""
        assert route_after_classify({"category": category}) == "plan"


class TestToolRouting:
    def test_fans_out_only_to_the_groups_the_plan_needs(self):
        state = {
            "pending_tool_calls": [
                {"tool": "retrieve_documents", "group": "rag"},
                {"tool": "search_clinical_trials", "group": "external"},
            ]
        }
        assert set(route_tools(state)) == {"rag_tools", "external_tools"}

    def test_single_group_does_not_start_the_others(self):
        state = {"pending_tool_calls": [{"tool": "get_experiment", "group": "sql"}]}
        assert route_tools(state) == ["sql_tools"]

    def test_sensitive_call_pauses_before_any_tool_runs(self):
        state = {
            "pending_tool_calls": [
                {"tool": "retrieve_documents", "group": "rag"},
                {"tool": "request_sensitive_action", "group": "sensitive"},
            ]
        }
        # The RAG branch must NOT start: approval comes first.
        assert route_tools(state) == ["approval_gate"]

    def test_empty_plan_still_reaches_synthesis(self):
        assert route_tools({"pending_tool_calls": []}) == ["analyze"]

    def test_budget_exhaustion_short_circuits_to_finalize(self):
        state = {
            "status": AgentRunStatus.BUDGET_EXCEEDED.value,
            "pending_tool_calls": [{"tool": "retrieve_documents", "group": "rag"}],
        }
        assert route_tools(state) == ["finalize"]


class TestVerifyRouting:
    def test_accept_finalizes(self):
        assert route_after_verify({"verification": {"recommendation": "accept"}}) == "finalize"

    def test_refuse_finalizes(self):
        assert route_after_verify({"verification": {"recommendation": "refuse"}}) == "finalize"

    def test_insufficient_evidence_retries(self):
        assert (
            route_after_verify({"verification": {"recommendation": "retrieve_more"}})
            == "retrieve_more"
        )

    def test_failed_run_never_retries(self):
        state = {
            "status": AgentRunStatus.FAILED.value,
            "verification": {"recommendation": "retrieve_more"},
        }
        assert route_after_verify(state) == "finalize"


class TestApprovalRouting:
    def test_rejection_finalizes_without_acting(self):
        assert route_after_approval({"status": AgentRunStatus.REJECTED.value}) == "finalize"

    def test_approval_proceeds_to_the_sensitive_tool(self):
        assert route_after_approval({"status": "running"}) == "sensitive_tools"


class TestAnalyzeRouting:
    def test_synthesis_failure_goes_to_the_failure_node(self):
        assert route_after_analyze({"status": AgentRunStatus.FAILED.value}) == "handle_failure"

    def test_success_goes_to_verification(self):
        assert route_after_analyze({"status": "running"}) == "verify"


class TestToolGrouping:
    @pytest.mark.parametrize(
        "tool,group",
        [
            ("retrieve_documents", "rag"),
            ("search_literature", "rag"),
            ("get_experiment", "sql"),
            ("search_compounds", "sql"),
            ("search_clinical_trials", "external"),
            ("mcp_search_research_data", "external"),
            ("request_sensitive_action", "sensitive"),
            ("made_up_tool", "unknown"),
        ],
    )
    def test_tool_group(self, tool, group):
        assert tool_group(tool) == group
