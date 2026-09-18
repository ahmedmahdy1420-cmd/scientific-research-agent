"""Evaluation dataset and results.

Evaluation cases live in the database (not only in a JSON file) so that new
cases can be added from real production runs: an agent run that a reviewer
marks as wrong becomes a regression case.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EvaluationCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_evaluation_cases_slug"),
        Index("ix_evaluation_cases_suite_enabled", "suite", "enabled"),
    )

    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    suite: Mapped[str] = mapped_column(String(64), default="core", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: Free-text description of what a correct run looks like (judge input).
    expected_behavior: Mapped[str] = mapped_column(Text, default="")
    expected_category: Mapped[str | None] = mapped_column(String(32))
    expected_tools: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    forbidden_tools: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    expected_tool_arguments: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: Substrings that must appear in at least one retrieved evidence snippet.
    expected_evidence_keywords: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    expected_answer_keywords: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    forbidden_answer_keywords: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    requires_citations: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_latency_ms: Mapped[int | None] = mapped_column(Integer)
    max_cost_usd: Mapped[float | None] = mapped_column(Float)
    as_role: Mapped[str] = mapped_column(String(32), default="researcher", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")

    results: Mapped[list[EvaluationResult]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<EvaluationCase {self.slug}>"


class EvaluationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (
        Index("ix_evaluation_results_run_label_created_at", "run_label", "created_at"),
        Index("ix_evaluation_results_case_id_created_at", "case_id", "created_at"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_cases.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    #: Groups every case in one evaluation invocation, e.g. "2026-09-18T10:00".
    run_label: Mapped[str] = mapped_column(String(128), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Deterministic checks: tool selection, citations, scope, keywords.
    deterministic_scores: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: LLM-as-a-judge rubric scores (0-1 per criterion) with written reasons.
    judge_scores: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    judge_reasoning: Mapped[str | None] = mapped_column(Text)
    overall_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    failures: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tools_used: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    #: Set when a human reviewed this result (the judge is not the final word).
    human_verdict: Mapped[str | None] = mapped_column(String(16))
    human_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[EvaluationCase] = relationship(back_populates="results", lazy="joined")
