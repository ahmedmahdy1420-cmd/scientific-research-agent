"""Structured-research read models."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.models.enums import EvidenceStrength, ExperimentStatus
from app.schemas.common import ORMModel


class CompoundResponse(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    iupac_name: str | None
    smiles: str | None
    molecular_weight: float | None
    mechanism_of_action: str | None
    therapeutic_area: str | None
    development_phase: str | None
    targets: list[str]


class ExperimentResultResponse(ORMModel):
    id: uuid.UUID
    metric_name: str
    metric_value: float
    unit: str | None
    p_value: float | None
    confidence_interval: str | None
    evidence_strength: EvidenceStrength
    conclusion: str


class ExperimentResponse(ORMModel):
    id: uuid.UUID
    code: str
    title: str
    hypothesis: str
    methodology: str
    status: ExperimentStatus
    access_level: str
    model_system: str | None
    sample_size: int | None
    started_on: dt.date | None
    completed_on: dt.date | None
    lead_researcher: str | None
    compound: CompoundResponse | None = None
    results: list[ExperimentResultResponse] = Field(default_factory=list)


class ResearchTopicResponse(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str
    research_area: str
    keywords: list[str]


class ResearchTopicDetailResponse(ResearchTopicResponse):
    experiments: list[ExperimentResponse] = Field(default_factory=list)
    document_count: int = 0


class CompoundSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=255)
    therapeutic_area: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=10, ge=1, le=50)
