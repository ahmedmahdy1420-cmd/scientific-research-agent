"""Shared response envelopes."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """Base for models populated from SQLAlchemy rows."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str | None = None


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PaginationParams(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    environment: str
    time: dt.datetime


class ReadinessResponse(BaseModel):
    """Liveness says 'the process is up'; readiness says 'it can serve traffic'."""

    status: str
    checks: dict[str, bool]
    degraded: list[str] = Field(default_factory=list)
