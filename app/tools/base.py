"""Tool contract.

A tool is a *typed, authorised, audited, bounded* function. Four properties,
each of which exists because of a specific failure mode:

* **typed** - arguments are a Pydantic model, so an argument the model invented
  is rejected before anything runs;
* **authorised** - `required_permission` is checked against the authenticated
  principal by the registry, never by the model;
* **audited** - every call, including denied ones, becomes a `tool_audit_logs`
  row;
* **bounded** - every tool declares a timeout and a result cap, so one slow
  dependency cannot hang an agent run.

Note `ToolContext` carries a session *factory*, not a session. Tools are run
concurrently with `asyncio.gather`, and a single `AsyncSession` is not safe to
share across concurrent tasks; each execution opens its own.
"""

from __future__ import annotations

import abc
import dataclasses
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.permissions import Principal
from app.llm.base import ToolSchema
from app.schemas.agent import EvidenceItem


@dataclasses.dataclass(slots=True)
class ToolContext:
    """Everything a tool may know about the caller. Note what is absent: the
    conversation, the prompt, and any model output."""

    principal: Principal
    session_factory: async_sessionmaker[AsyncSession]
    run_id: uuid.UUID | None = None
    request_id: str | None = None
    #: Set by the registry when a sensitive action has been human-approved.
    approved_actions: frozenset[str] = frozenset()


@dataclasses.dataclass(slots=True)
class ToolResult:
    """Uniform result envelope. Tools never raise into the graph."""

    tool: str
    ok: bool
    summary: str
    data: Any = None
    evidence: list[EvidenceItem] = dataclasses.field(default_factory=list)
    count: int = 0
    latency_ms: int = 0
    retry_count: int = 0
    cache_hit: bool = False
    degraded: bool = False
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def failure(
        cls,
        tool: str,
        code: str,
        message: str,
        *,
        latency_ms: int = 0,
        retry_count: int = 0,
    ) -> ToolResult:
        return cls(
            tool=tool,
            ok=False,
            summary=f"{tool} failed: {message}",
            error_code=code,
            error_message=message,
            latency_ms=latency_ms,
            retry_count=retry_count,
        )

    def to_model_payload(self) -> dict[str, Any]:
        """What the model is allowed to see about this call.

        Errors are surfaced as a short code and message, never as a stack trace
        or a raw upstream body: those leak internals and are an injection
        vector in their own right.
        """
        if not self.ok:
            return {
                "tool": self.tool,
                "ok": False,
                "error": self.error_code,
                "message": self.error_message,
            }
        return {
            "tool": self.tool,
            "ok": True,
            "count": self.count,
            "summary": self.summary,
            "results": self.data,
            "degraded": self.degraded,
        }


class Tool(abc.ABC):
    """Base class for every tool in the registry."""

    #: Stable identifier the model uses. Also the audit-log key.
    name: str = ""
    description: str = ""
    input_model: type[BaseModel]
    #: Permission the principal must hold. None = any authenticated caller.
    required_permission: str | None = None
    #: Sensitive tools cannot execute without an explicit human approval.
    sensitive: bool = False
    #: Safe to cache: output depends only on the arguments, not on the caller.
    #: Anything filtered by the principal's access level must stay False, or
    #: one user's cache entry could be served to another.
    cacheable: bool = False
    cache_ttl_seconds: int = 900
    timeout_seconds: float = 20.0

    @abc.abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        """Execute with already-validated arguments."""

    # ----------------------------------------------------------------- schema
    def json_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def to_llm_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters=self.json_schema()
        )

    def validate_arguments(self, raw: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(raw)

    def __repr__(self) -> str:
        return f"<Tool {self.name}>"
