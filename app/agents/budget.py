"""Bounded autonomy.

An agent that can loop is an agent that can loop forever, on someone else's
money. Three independent ceilings, all configurable, all checked before work is
started rather than after it has been paid for:

* `MAX_AGENT_ITERATIONS` - how many times the verify -> retrieve-more cycle may
  run. Without it, "the evidence is insufficient" is an infinite loop.
* `MAX_TOOL_CALLS` - total tool executions per run, across all iterations.
* `MAX_RUNTIME_SECONDS` - wall-clock deadline. This is the one that protects the
  user-facing request, because the other two can both be satisfied while a
  single slow dependency hangs.

Exceeding a budget is a *first-class outcome*, not an exception: the run ends
with `status=budget_exceeded` and returns whatever evidence it did gather,
with the truncation stated in the answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    ok: bool
    reason: str | None = None
    remaining_tool_calls: int = 0
    seconds_left: float = 0.0


@dataclass(slots=True)
class AgentBudget:
    max_iterations: int
    max_tool_calls: int
    max_runtime_seconds: float
    started_at: float

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
    ) -> AgentBudget:
        cfg = settings or get_settings()
        return cls(
            max_iterations=min(max_iterations or cfg.max_agent_iterations, 5),
            max_tool_calls=min(max_tool_calls or cfg.max_tool_calls, 20),
            max_runtime_seconds=cfg.max_runtime_seconds,
            started_at=time.monotonic(),
        )

    @property
    def deadline(self) -> float:
        return self.started_at + self.max_runtime_seconds

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def check(self, *, iterations: int, tool_calls_used: int) -> BudgetVerdict:
        seconds_left = self.deadline - time.monotonic()
        if seconds_left <= 0:
            return BudgetVerdict(
                False, f"runtime budget of {self.max_runtime_seconds:.0f}s exhausted"
            )
        if tool_calls_used >= self.max_tool_calls:
            return BudgetVerdict(
                False,
                f"tool-call budget of {self.max_tool_calls} exhausted",
                seconds_left=seconds_left,
            )
        if iterations >= self.max_iterations:
            return BudgetVerdict(
                False,
                f"iteration budget of {self.max_iterations} exhausted",
                seconds_left=seconds_left,
            )
        return BudgetVerdict(
            True,
            remaining_tool_calls=self.max_tool_calls - tool_calls_used,
            seconds_left=seconds_left,
        )


def deadline_exceeded(deadline: float) -> bool:
    """Wall-clock check usable from checkpointed state (uses time.time())."""
    return deadline > 0 and time.time() > deadline
