"""MCP client used by the agent to call the MCP server.

This is the "how does an agent connect to MCP" half of the story. The client
connects over streamable HTTP with a bearer token, lists the server's tools
(schemas are discovered at runtime, not hard-coded), and calls them.

Failure policy: MCP is treated as any other remote dependency. Connection
errors are caught and returned as a failed `ToolResult`, never raised into the
graph, and `MCP_ENABLED=false` removes the tools from the registry entirely so
a deployment without an MCP server simply advertises fewer tools.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

log = get_logger(__name__)


class BearerTokenTransport:
    """Streamable-HTTP MCP transport that authenticates every request.

    `Client(url)` builds an unauthenticated transport and the transport class
    itself takes only a URL, so the token is attached through the HTTP client
    the SDK exposes for exactly this purpose. A production deployment would
    swap the static bearer for an OAuth2 access token issued by the same
    provider as the REST API; only this class would change.
    """

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Any:
        self._stack = AsyncExitStack()
        http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {self._token}"})
        await self._stack.enter_async_context(http_client)
        return await self._stack.enter_async_context(
            streamable_http_client(self._url, http_client=http_client)
        )

    async def __aexit__(self, *exc_info: Any) -> bool | None:
        if self._stack is None:
            return None
        stack, self._stack = self._stack, None
        return await stack.__aexit__(*exc_info)


class MCPToolClient:
    """Thin wrapper over the official MCP SDK client."""

    def __init__(self, settings: Settings | None = None, url: str | None = None) -> None:
        self._settings = settings or get_settings()
        self._url = url or self._settings.mcp_server_url

    def _build(self) -> Client:
        """Fresh client per call.

        MCP sessions are stateful and not safe to share across the concurrent
        tool calls the agent fans out; a short-lived session per call is the
        simpler correct choice at this scale. A high-throughput deployment
        would pool sessions instead.
        """
        return Client(
            BearerTokenTransport(self._url, self._settings.mcp_auth_token),
            read_timeout_seconds=20.0,
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        """Runtime tool discovery - the reason MCP exists."""
        try:
            async with self._build() as client:
                result = await client.list_tools()
        except Exception as exc:
            raise ExternalServiceError(f"Could not list MCP tools: {exc}") from exc

        tools: list[Any] = list(getattr(result, "tools", result) or [])
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema,
                "output_schema": getattr(tool, "output_schema", None),
            }
            for tool in tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and return its structured result.

        MCP returns both `content` (text blocks, for a human or a model) and
        `structured_content` (JSON matching the tool's declared output schema).
        We prefer the structured form and fall back to parsing the text.
        """
        try:
            async with self._build() as client:
                result = await client.call_tool(name, arguments)
        except Exception as exc:
            log.warning("mcp.call_failed", tool=name, error=str(exc))
            raise ExternalServiceError(f"MCP call to {name!r} failed: {exc}") from exc

        if getattr(result, "is_error", False):
            text = _first_text(result)
            log.warning("mcp.tool_error", tool=name, detail=text[:300])
            raise ExternalServiceError(f"MCP tool {name!r} reported an error: {text[:300]}")

        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured

        text = _first_text(result)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except json.JSONDecodeError:
            return {"result": text}

    async def health(self) -> bool:
        try:
            await self.list_tools()
            return True
        except Exception:  # noqa: BLE001 - probe never raises
            return False


def _first_text(result: Any) -> str:
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return str(text)
    return ""


_client: MCPToolClient | None = None


def get_mcp_client() -> MCPToolClient:
    global _client
    if _client is None:
        _client = MCPToolClient()
    return _client


def set_mcp_client(client: MCPToolClient | None) -> None:
    """Test hook: inject an in-process client bound to the server object."""
    global _client
    _client = client
