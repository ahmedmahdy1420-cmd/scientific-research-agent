"""MCP server and client.

Runs the real MCP server object in-process through the official SDK's `Client`,
so the protocol path (tool discovery, JSON Schema, structured results, error
handling) is genuinely exercised without needing a listening socket.
"""

from __future__ import annotations

import pytest
from mcp import Client

from app.mcp.server import server as mcp_server
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

EXPECTED_TOOLS = {
    "search_research_data",
    "get_experiment",
    "search_literature",
    "get_trial_information",
}


class TestDiscovery:
    async def test_all_documented_tools_are_advertised(self):
        async with Client(mcp_server) as client:
            result = await client.list_tools()
        assert {t.name for t in result.tools} == EXPECTED_TOOLS

    async def test_tools_publish_input_and_output_schemas(self):
        """Runtime schema discovery is the point of MCP: a client that has
        never seen this server can still call it correctly."""
        async with Client(mcp_server) as client:
            result = await client.list_tools()
        for tool in result.tools:
            assert tool.description, tool.name
            assert tool.input_schema["type"] == "object"
            assert tool.output_schema is not None, tool.name

    async def test_experiment_tool_schema_is_specific(self):
        async with Client(mcp_server) as client:
            result = await client.list_tools()
        tool = next(t for t in result.tools if t.name == "get_experiment")
        assert "experiment_id_or_code" in tool.input_schema["properties"]
        assert tool.input_schema["required"] == ["experiment_id_or_code"]


class TestToolExecution:
    async def test_search_research_data_returns_structured_content(self, research_corpus):
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "search_research_data",
                {"query": "breast cancer HER2", "entity_type": "all", "limit": 5},
            )
        assert result.is_error is False
        payload = result.structured_content
        assert payload["count"] >= 1
        assert isinstance(payload["documents"], list)

    async def test_get_experiment_returns_results(self, research_corpus):
        async with Client(mcp_server) as client:
            result = await client.call_tool("get_experiment", {"experiment_id_or_code": "EXP-0001"})
        payload = result.structured_content
        assert payload["found"] is True
        assert payload["code"] == "EXP-0001"
        assert payload["results"][0]["metric"] == "IC50"

    async def test_missing_experiment_is_a_clean_not_found(self, research_corpus):
        async with Client(mcp_server) as client:
            result = await client.call_tool("get_experiment", {"experiment_id_or_code": "EXP-NOPE"})
        assert result.is_error is False
        assert result.structured_content["found"] is False

    async def test_search_literature_returns_citable_passages(self, research_corpus):
        async with Client(mcp_server) as client:
            result = await client.call_tool(
                "search_literature", {"query": "PARP inhibition", "limit": 3}
            )
        payload = result.structured_content
        assert payload["count"] >= 1
        assert payload["passages"][0]["source_id"].startswith("doc:")


class TestMCPSecurity:
    async def test_invalid_arguments_are_rejected_by_the_protocol(self, research_corpus):
        async with Client(mcp_server) as client:
            result = await client.call_tool("search_research_data", {"query": "x", "limit": 9999})
        assert result.is_error is True

    async def test_missing_required_argument_rejected(self, research_corpus):
        async with Client(mcp_server) as client:
            result = await client.call_tool("get_experiment", {})
        assert result.is_error is True

    async def test_service_principal_cannot_read_restricted_material(self, research_corpus):
        """The MCP service account is deliberately limited. A leaked MCP token
        must not expose restricted documents or experiments."""
        async with Client(mcp_server) as client:
            literature = await client.call_tool(
                "search_literature",
                {"query": "internal safety review serious adverse events", "limit": 20},
            )
            experiment = await client.call_tool(
                "get_experiment", {"experiment_id_or_code": "EXP-9999"}
            )

        blob = " ".join(p["snippet"] for p in literature.structured_content["passages"])
        assert "three unpublished" not in blob
        assert experiment.structured_content["found"] is False

    def test_service_principal_holds_no_write_or_sensitive_permissions(self):
        from app.auth.permissions import Permission
        from app.mcp.server import MCP_SERVICE_PRINCIPAL

        for forbidden in (
            Permission.TOOLS_SENSITIVE,
            Permission.DOCUMENTS_DELETE,
            Permission.DOCUMENTS_READ_RESTRICTED,
            Permission.EXPERIMENTS_READ_RESTRICTED,
            Permission.APPROVALS_DECIDE,
            Permission.USERS_MANAGE,
        ):
            assert not MCP_SERVICE_PRINCIPAL.has_permission(forbidden), forbidden


class TestAgentMCPTools:
    async def test_agent_mcp_tool_maps_results_to_evidence(self, research_corpus, researcher):
        """The agent-side tool wrapper, driven against an in-process client."""
        from app.database.session import get_async_session_factory
        from app.mcp.client import MCPToolClient
        from app.tools.base import ToolContext
        from app.tools.mcp_tools import MCPSearchRequest, MCPSearchResearchDataTool

        class InProcessClient(MCPToolClient):
            def _build(self):  # type: ignore[override]
                return Client(mcp_server)

        tool = MCPSearchResearchDataTool(InProcessClient())
        result = await tool.run(
            MCPSearchRequest(query="breast cancer HER2", entity_type="all", limit=5),
            ToolContext(principal=researcher, session_factory=get_async_session_factory()),
        )
        assert result.ok
        assert result.count >= 1
        assert all(e.origin_tool == "mcp_search_research_data" for e in result.evidence)

    async def test_mcp_outage_degrades_rather_than_crashing(self, researcher):
        from app.core.errors import ExternalServiceError
        from app.database.session import get_async_session_factory
        from app.mcp.client import MCPToolClient
        from app.tools.base import ToolContext
        from app.tools.mcp_tools import MCPSearchRequest, MCPSearchResearchDataTool

        class BrokenClient(MCPToolClient):
            async def call_tool(self, name, arguments):  # type: ignore[override]
                raise ExternalServiceError("MCP server unreachable")

        tool = MCPSearchResearchDataTool(BrokenClient())
        result = await tool.run(
            MCPSearchRequest(query="anything"),
            ToolContext(principal=researcher, session_factory=get_async_session_factory()),
        )
        assert not result.ok
        assert result.error_code == "mcp_unavailable"
