"""Evaluation-specific structured outputs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JudgeVerdict(BaseModel):
    """LLM-as-a-judge rubric. Every criterion is 0.0-1.0 with a written reason.

    The reasoning field comes *first* in the schema deliberately: with ordered
    JSON generation, making the model write its justification before its scores
    produces measurably better-calibrated scores than the reverse.
    """

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(description="Quote the text that drove each score")
    grounding: float = Field(ge=0.0, le=1.0)
    relevance: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    citation_quality: float = Field(ge=0.0, le=1.0)
    scope_adherence: float = Field(ge=0.0, le=1.0)

    @property
    def mean(self) -> float:
        return round(
            (
                self.grounding
                + self.relevance
                + self.completeness
                + self.citation_quality
                + self.scope_adherence
            )
            / 5,
            4,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "grounding": self.grounding,
            "relevance": self.relevance,
            "completeness": self.completeness,
            "citation_quality": self.citation_quality,
            "scope_adherence": self.scope_adherence,
            "mean": self.mean,
        }
