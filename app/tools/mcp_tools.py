"""Agent tools that reach the MCP server.

These are intentionally thin. They exist so the agent exercises the *same*
MCP surface an external client would use — proving the protocol path works
end to end rather than mocking it — while the authorisation, validation,
timeout and audit guarantees still come from the local tool registry.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.auth.permissions import Permission
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.mcp.client import MCPToolClient, get_mcp_client
from app.schemas.agent import EvidenceItem
from app.tools.base import Tool, ToolContext, ToolResult

log = get_logger(__name__)


class MCPSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    entity_type: Literal["all", "documents", "experiments", "compounds"] = "all"
    limit: int = Field(default=8, ge=1, le=25)


class MCPSearchResearchDataTool(Tool):
    name = "mcp_search_research_data"
    description = (
        "Cross-entity research search over MCP: documents, experiments and "
        "compounds in one call. Use when a question spans more than one kind of "
        "record and you want a single sweep."
    )
    input_model = MCPSearchRequest
    required_permission = Permission.EXPERIMENTS_READ
    timeout_seconds = 25.0

    def __init__(self, client: MCPToolClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> MCPToolClient:
        return self._client or get_mcp_client()

    async def run(self, args: MCPSearchRequest, ctx: ToolContext) -> ToolResult:
        try:
            payload = await self.client.call_tool(
                "search_research_data",
                {"query": args.query, "entity_type": args.entity_type, "limit": args.limit},
            )
        except ExternalServiceError as exc:
            return ToolResult.failure(self.name, "mcp_unavailable", exc.message)

        evidence: list[EvidenceItem] = []
        for doc in payload.get("documents", []):
            evidence.append(
                EvidenceItem(
                    source_type="document",
                    source_id=doc.get("source_id", "doc:unknown"),
                    title=doc.get("title", "Untitled"),
                    snippet=(doc.get("snippet") or "")[:1200],
                    origin_tool=self.name,
                    relevance_score=doc.get("relevance"),
                )
            )
        for exp in payload.get("experiments", []):
            evidence.append(
                EvidenceItem(
                    source_type="experiment",
                    source_id=f"exp:{exp.get('code')}",
                    title=exp.get("title", "Untitled experiment"),
                    snippet=(
                        f"{exp.get('title')}. Compound: {exp.get('compound') or 'n/a'}. "
                        f"Model system: {exp.get('model_system') or 'n/a'}, "
                        f"n={exp.get('sample_size') or 'n/a'}. Status: {exp.get('status')}."
                    )[:1200],
                    origin_tool=self.name,
                )
            )
        for cmp_ in payload.get("compounds", []):
            evidence.append(
                EvidenceItem(
                    source_type="compound",
                    source_id=f"cmp:{cmp_.get('code')}",
                    title=f"{cmp_.get('name')} ({cmp_.get('code')})",
                    snippet=(
                        f"{cmp_.get('name')} - mechanism: "
                        f"{cmp_.get('mechanism_of_action') or 'n/a'}; targets: "
                        f"{', '.join(cmp_.get('targets') or []) or 'n/a'}."
                    )[:1200],
                    origin_tool=self.name,
                )
            )

        count = int(payload.get("count", len(evidence)))
        return ToolResult(
            tool=self.name,
            ok=True,
            summary=f"MCP returned {count} record(s) for {args.query!r}.",
            data=payload,
            evidence=evidence,
            count=count,
        )


class MCPTrialRequest(BaseModel):
    registry_id: str = Field(
        min_length=3, max_length=64, description="Trial registry id, e.g. NCT04123456"
    )


class MCPGetTrialInformationTool(Tool):
    name = "mcp_get_trial_information"
    description = "Fetch one clinical trial's full record by registry id, over MCP."
    input_model = MCPTrialRequest
    required_permission = Permission.SEARCH_TRIALS
    cacheable = True
    timeout_seconds = 25.0

    def __init__(self, client: MCPToolClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> MCPToolClient:
        return self._client or get_mcp_client()

    async def run(self, args: MCPTrialRequest, ctx: ToolContext) -> ToolResult:
        try:
            payload = await self.client.call_tool(
                "get_trial_information", {"registry_id": args.registry_id}
            )
        except ExternalServiceError as exc:
            return ToolResult.failure(self.name, "mcp_unavailable", exc.message)

        if not payload.get("found"):
            return ToolResult(
                tool=self.name,
                ok=True,
                summary=f"No trial found with registry id {args.registry_id!r}.",
                data=None,
                count=0,
            )

        evidence = EvidenceItem(
            source_type="clinical_trial",
            source_id=f"trial:{payload.get('registry_id')}",
            title=payload.get("title") or args.registry_id,
            snippet=(
                f"{payload.get('title')} - {payload.get('phase')}, status "
                f"{payload.get('status')}, condition {payload.get('condition')}, "
                f"enrolment {payload.get('enrollment') or 'n/a'}. "
                f"Primary outcome: {payload.get('primary_outcome') or 'n/a'}. "
                f"{payload.get('summary') or ''}"
            )[:1200],
            origin_tool=self.name,
        )
        return ToolResult(
            tool=self.name,
            ok=True,
            summary=f"Trial {payload.get('registry_id')}: {payload.get('title')}",
            data=payload,
            evidence=[evidence],
            count=1,
            degraded=payload.get("source") == "database_cache",
        )
