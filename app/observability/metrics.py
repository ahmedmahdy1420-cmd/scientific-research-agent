"""Application metrics.

Emitted as structured log events (`metric.*`) rather than to a metrics backend.
That is a deliberate scope decision: in the AWS design these lines are picked up
by CloudWatch Logs and turned into metrics by metric filters, which needs no
extra infrastructure. Swapping in a Prometheus client later means changing this
one file.

The four things actually worth alerting on for an agent service:

* answer latency (p95) - user-visible
* cost per run - the bill
* tool failure rate by tool - which dependency is degrading
* grounding failure rate - the quality regression signal that latency and
  error-rate dashboards cannot see
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

log = get_logger("metrics")


def counter(name: str, value: int = 1, **tags: Any) -> None:
    log.info("metric.counter", metric=name, value=value, **tags)


def gauge(name: str, value: float, **tags: Any) -> None:
    log.info("metric.gauge", metric=name, value=value, **tags)


def timing(name: str, milliseconds: int, **tags: Any) -> None:
    log.info("metric.timing", metric=name, value_ms=milliseconds, **tags)


@dataclass(slots=True)
class RunMetrics:
    """Accumulates one agent run's numbers, emitted once at the end.

    One wide event per run beats a dozen narrow ones: it can be queried in
    CloudWatch Logs Insights without joins.
    """

    run_id: str
    user_id: str
    category: str | None = None
    status: str = "unknown"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: int = 0
    tool_latency_ms: int = 0
    retrieval_latency_ms: int = 0
    total_latency_ms: int = 0
    iterations: int = 0
    tool_calls: int = 0
    cache_hits: int = 0
    tools_used: list[str] = field(default_factory=list)
    tool_failures: list[str] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)
    grounded: bool | None = None
    citation_count: int = 0

    def emit(self) -> None:
        log.info(
            "metric.agent_run",
            run_id=self.run_id,
            user_id=self.user_id,
            category=self.category,
            status=self.status,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.prompt_tokens + self.completion_tokens,
            cost_usd=round(self.cost_usd, 6),
            llm_latency_ms=self.llm_latency_ms,
            tool_latency_ms=self.tool_latency_ms,
            retrieval_latency_ms=self.retrieval_latency_ms,
            total_latency_ms=self.total_latency_ms,
            iterations=self.iterations,
            tool_calls=self.tool_calls,
            cache_hits=self.cache_hits,
            tools_used=self.tools_used,
            tool_failures=self.tool_failures,
            models_used=self.models_used,
            grounded=self.grounded,
            citation_count=self.citation_count,
        )
