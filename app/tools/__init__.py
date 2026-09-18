"""Tool layer: typed, authorised, audited, bounded."""

from app.core.config import Settings, get_settings
from app.core.redis import RedisCache
from app.tools.base import Tool, ToolContext, ToolResult
from app.tools.clinical_trials import SearchClinicalTrialsTool
from app.tools.literature import RetrieveDocumentsTool, SearchLiteratureTool
from app.tools.mcp_tools import MCPGetTrialInformationTool, MCPSearchResearchDataTool
from app.tools.registry import ToolRegistry
from app.tools.sensitive import RequestSensitiveActionTool
from app.tools.sql_tools import (
    GetExperimentResultsTool,
    GetExperimentTool,
    SearchCompoundsTool,
    SearchExperimentsByCompoundTool,
)

_registry: ToolRegistry | None = None


def build_tool_registry(
    settings: Settings | None = None, cache: RedisCache | None = None
) -> ToolRegistry:
    """Assemble the tool allowlist.

    MCP tools are only registered when `MCP_ENABLED` is set: a deployment
    without an MCP server should advertise fewer tools, not advertise tools
    that always fail.
    """
    cfg = settings or get_settings()
    tools: list[Tool] = [
        RetrieveDocumentsTool(),
        SearchLiteratureTool(),
        SearchClinicalTrialsTool(),
        GetExperimentTool(),
        GetExperimentResultsTool(),
        SearchCompoundsTool(),
        SearchExperimentsByCompoundTool(),
        RequestSensitiveActionTool(),
    ]
    if cfg.mcp_enabled:
        tools.extend([MCPSearchResearchDataTool(), MCPGetTrialInformationTool()])
    return ToolRegistry(tools, settings=cfg, cache=cache)


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_tool_registry(cache=RedisCache())
    return _registry


def set_tool_registry(registry: ToolRegistry | None) -> None:
    """Test hook."""
    global _registry
    _registry = registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_tool_registry",
    "get_tool_registry",
    "set_tool_registry",
]
