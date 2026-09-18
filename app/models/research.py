"""Structured research domain: topics, compounds, experiments, trials.

This is the half of the system the LLM reaches through *typed SQL tools*
rather than through RAG. Questions like "which experiments used compound X"
have an exact answer in relational data; retrieving prose about them would be
strictly worse.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
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
from app.models.enums import (
    AccessLevel,
    EvidenceStrength,
    ExperimentStatus,
    TrialPhase,
    TrialStatus,
)


class ResearchTopic(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "research_topics"
    __table_args__ = (UniqueConstraint("slug", name="uq_research_topics_slug"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    research_area: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    experiments: Mapped[list[Experiment]] = relationship(back_populates="topic", lazy="noload")

    def __repr__(self) -> str:
        return f"<ResearchTopic {self.slug}>"


class Compound(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "compounds"
    __table_args__ = (
        UniqueConstraint("code", name="uq_compounds_code"),
        Index("ix_compounds_name_area", "name", "therapeutic_area"),
        CheckConstraint(
            "molecular_weight IS NULL OR molecular_weight > 0",
            name="molecular_weight_positive",
        ),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    iupac_name: Mapped[str | None] = mapped_column(String(512))
    smiles: Mapped[str | None] = mapped_column(String(1024))
    molecular_weight: Mapped[float | None] = mapped_column(Float)
    mechanism_of_action: Mapped[str | None] = mapped_column(Text)
    therapeutic_area: Mapped[str | None] = mapped_column(String(128), index=True)
    development_phase: Mapped[str | None] = mapped_column(String(64))
    targets: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    experiments: Mapped[list[Experiment]] = relationship(back_populates="compound", lazy="noload")

    def __repr__(self) -> str:
        return f"<Compound {self.code} {self.name}>"


class Experiment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("code", name="uq_experiments_code"),
        Index("ix_experiments_compound_id_status", "compound_id", "status"),
        Index("ix_experiments_topic_id_started_on", "topic_id", "started_on"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    methodology: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ExperimentStatus] = mapped_column(
        Enum(ExperimentStatus, native_enum=False, length=24),
        default=ExperimentStatus.COMPLETED,
        nullable=False,
    )
    access_level: Mapped[AccessLevel] = mapped_column(
        Enum(AccessLevel, native_enum=False, length=16),
        default=AccessLevel.INTERNAL,
        nullable=False,
        index=True,
    )
    model_system: Mapped[str | None] = mapped_column(String(128))
    sample_size: Mapped[int | None] = mapped_column(Integer)
    started_on: Mapped[dt.date | None] = mapped_column(Date)
    completed_on: Mapped[dt.date | None] = mapped_column(Date)
    lead_researcher: Mapped[str | None] = mapped_column(String(255))

    compound_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("compounds.id", ondelete="SET NULL"), index=True
    )
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_topics.id", ondelete="SET NULL"), index=True
    )

    compound: Mapped[Compound | None] = relationship(back_populates="experiments", lazy="joined")
    topic: Mapped[ResearchTopic | None] = relationship(back_populates="experiments", lazy="joined")
    results: Mapped[list[ExperimentResult]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<Experiment {self.code}>"


class ExperimentResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiment_results"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "metric_name", name="uq_experiment_results_experiment_id_metric_name"
        ),
        Index("ix_experiment_results_experiment_id", "experiment_id"),
        CheckConstraint(
            "p_value IS NULL OR (p_value >= 0 AND p_value <= 1)", name="p_value_in_range"
        ),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64))
    p_value: Mapped[float | None] = mapped_column(Float)
    confidence_interval: Mapped[str | None] = mapped_column(String(128))
    evidence_strength: Mapped[EvidenceStrength] = mapped_column(
        Enum(EvidenceStrength, native_enum=False, length=24),
        default=EvidenceStrength.MODERATE,
        nullable=False,
    )
    conclusion: Mapped[str] = mapped_column(Text, default="")

    experiment: Mapped[Experiment] = relationship(back_populates="results", lazy="joined")

    def __repr__(self) -> str:
        return f"<ExperimentResult {self.metric_name}={self.metric_value}>"


class ClinicalTrial(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Locally cached trial registry records.

    The authoritative source in this demo is the mock external REST API; this
    table is the internal mirror the SQL tools query, which lets the agent join
    trials against internal experiments without a network hop.
    """

    __tablename__ = "clinical_trials"
    __table_args__ = (
        UniqueConstraint("registry_id", name="uq_clinical_trials_registry_id"),
        Index("ix_clinical_trials_condition_phase", "condition", "phase"),
        Index("ix_clinical_trials_status_start_date", "status", "start_date"),
        CheckConstraint("enrollment IS NULL OR enrollment >= 0", name="enrollment_non_negative"),
    )

    registry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    condition: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    intervention: Mapped[str | None] = mapped_column(String(512))
    phase: Mapped[TrialPhase] = mapped_column(
        Enum(TrialPhase, native_enum=False, length=16), nullable=False
    )
    status: Mapped[TrialStatus] = mapped_column(
        Enum(TrialStatus, native_enum=False, length=32), nullable=False
    )
    sponsor: Mapped[str | None] = mapped_column(String(255))
    enrollment: Mapped[int | None] = mapped_column(Integer)
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    completion_date: Mapped[dt.date | None] = mapped_column(Date)
    primary_outcome: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    compound_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("compounds.id", ondelete="SET NULL"), index=True
    )

    def __repr__(self) -> str:
        return f"<ClinicalTrial {self.registry_id} {self.phase}>"
