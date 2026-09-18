"""Model routing.

The strongest model is not automatically the right one. Classification and
metadata extraction are short, schema-constrained and easy; a small model does
them at roughly a tenth of the price and a fraction of the latency, and the
structured-output schema catches the rare miss. Planning, synthesis and
verification stay on the reasoning model because a mistake there is a wrong
answer rather than a retry.

Every mapping is environment-driven, so routing can be re-tuned from evaluation
results without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.llm.base import TaskKind


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    model: str
    task: TaskKind
    reason: str


class ModelRouter:
    """Maps a logical task to a concrete model name."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def resolve(self, task: TaskKind) -> RoutingDecision:
        cfg = self._settings
        match task:
            case "reasoning":
                return RoutingDecision(
                    cfg.llm_reasoning_model, task, "multi-step reasoning or final synthesis"
                )
            case "fast":
                return RoutingDecision(
                    cfg.llm_fast_model, task, "short, schema-constrained, latency-sensitive"
                )
            case "judge":
                return RoutingDecision(
                    cfg.llm_judge_model, task, "evaluation grading needs the stronger model"
                )
            case "embedding":
                return RoutingDecision(cfg.embedding_model, task, "vector embedding")
        raise ValueError(f"Unknown task kind: {task}")

    def model_for(self, task: TaskKind) -> str:
        return self.resolve(task).model

    def fallback_for(self, task: TaskKind) -> str | None:
        """Cheaper model to retry on when the primary is rate limited or down.

        Degrading to a smaller model beats returning a 502: the answer is
        marked lower-confidence rather than lost.
        """
        cfg = self._settings
        if task in ("reasoning", "judge") and cfg.llm_fast_model != cfg.llm_reasoning_model:
            return cfg.llm_fast_model
        return None
