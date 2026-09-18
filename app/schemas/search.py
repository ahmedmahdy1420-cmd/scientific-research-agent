"""Search request/response schemas.

`LiteratureSearchRequest` and `ClinicalTrialSearchRequest` are used twice: as
the HTTP body for the REST endpoints *and* as the validation schema for the
matching agent tool. One definition, so an argument the model invents is
rejected by exactly the same rules a hand-written API call would face.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import DocumentType, TrialPhase, TrialStatus


class LiteratureSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    research_area: str | None = Field(default=None, max_length=128)
    document_types: list[DocumentType] | None = None
    sources: list[str] | None = Field(default=None, max_length=10)
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _check_date_range(self) -> LiteratureSearchRequest:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


class RetrievedChunk(BaseModel):
    chunk_id: uuid.UUID
    chunk_index: int
    document_id: uuid.UUID
    document_title: str
    content: str
    similarity: float
    page_number: int | None = None
    section: str | None = None
    publication_date: dt.date | None = None
    research_area: str | None = None
    document_type: str | None = None
    source: str | None = None
    doi: str | None = None
    rerank_score: float | None = None


class LiteratureSearchResponse(BaseModel):
    query: str
    results: list[RetrievedChunk]
    total: int
    latency_ms: int
    cache_hit: bool = False
    reranked: bool = False
    filters_applied: dict[str, Any] = Field(default_factory=dict)


class ClinicalTrialSearchRequest(BaseModel):
    condition: str = Field(min_length=2, max_length=255)
    phase: TrialPhase | None = None
    status: TrialStatus | None = None
    intervention: str | None = Field(default=None, max_length=255)
    min_enrollment: int | None = Field(default=None, ge=0)
    limit: int = Field(default=10, ge=1, le=50)


class ClinicalTrialResult(BaseModel):
    registry_id: str
    title: str
    condition: str
    intervention: str | None = None
    phase: str
    status: str
    sponsor: str | None = None
    enrollment: int | None = None
    start_date: dt.date | None = None
    completion_date: dt.date | None = None
    primary_outcome: str | None = None
    summary: str | None = None
    locations: list[str] = Field(default_factory=list)


class ClinicalTrialSearchResponse(BaseModel):
    condition: str
    results: list[ClinicalTrialResult]
    total: int
    source: Literal["external_api", "database_cache", "stale_cache"]
    latency_ms: int
    degraded: bool = False
    degraded_reason: str | None = None
