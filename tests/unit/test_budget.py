"""Bounded agent autonomy."""

from __future__ import annotations

import time

import pytest

from app.agents.budget import AgentBudget, deadline_exceeded
from app.core.config import get_settings

pytestmark = pytest.mark.unit


class TestBudget:
    def test_fresh_budget_permits_work(self):
        budget = AgentBudget.create(get_settings())
        verdict = budget.check(iterations=0, tool_calls_used=0)
        assert verdict.ok
        assert verdict.remaining_tool_calls > 0

    def test_tool_call_ceiling_enforced(self):
        budget = AgentBudget.create(get_settings(), max_tool_calls=3)
        assert budget.check(iterations=0, tool_calls_used=2).ok
        verdict = budget.check(iterations=0, tool_calls_used=3)
        assert not verdict.ok
        assert "tool-call budget" in verdict.reason

    def test_iteration_ceiling_enforced(self):
        budget = AgentBudget.create(get_settings(), max_iterations=2)
        assert budget.check(iterations=1, tool_calls_used=0).ok
        verdict = budget.check(iterations=2, tool_calls_used=0)
        assert not verdict.ok
        assert "iteration budget" in verdict.reason

    def test_runtime_ceiling_enforced(self):
        budget = AgentBudget.create(get_settings())
        budget.max_runtime_seconds = 0.0
        budget.started_at = time.monotonic() - 1
        verdict = budget.check(iterations=0, tool_calls_used=0)
        assert not verdict.ok
        assert "runtime budget" in verdict.reason

    def test_client_cannot_raise_the_ceiling_above_the_deployment_maximum(self):
        """A caller asking for 500 iterations is clamped, not obeyed."""
        budget = AgentBudget.create(get_settings(), max_iterations=500, max_tool_calls=500)
        assert budget.max_iterations <= 5
        assert budget.max_tool_calls <= 20

    def test_deadline_helper(self):
        assert deadline_exceeded(time.time() - 1)
        assert not deadline_exceeded(time.time() + 60)
        assert not deadline_exceeded(0.0)  # 0 means "no deadline set"
