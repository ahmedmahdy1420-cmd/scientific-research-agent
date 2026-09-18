"""The tool registry: the authorisation and audit choke point.

Read this file as the answer to "how do you stop the LLM doing something it
shouldn't". Every tool call, whoever proposed it, goes through `ToolRegistry.call`:

    1. Is this a tool that exists?            -> allowlist, not free-form dispatch
    2. May THIS principal use it?             -> permission check in Python
    3. Are the arguments valid?               -> Pydantic validation
    4. Is it sensitive and unapproved?        -> refuse, pause for a human
    5. Cache lookup (only where safe)
    6. Execute under a timeout
    7. Write an audit row - including for denials

The model's opinion is an input to step 0 and is consulted nowhere else. There
is no code path where a string produced by a model, or found in a document,
can cause step 2 to be skipped.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from pydantic import ValidationError

from app.auth.permissions import SENSITIVE_TOOLS, Principal
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.redis import CacheKey, RedisCache
from app.database.session import session_scope
from app.llm.base import ToolSchema
from app.models.agent import ToolAuditLog
from app.models.enums import ToolCallStatus
from app.schemas.agent import EvidenceItem
from app.tools.base import Tool, ToolContext, ToolResult

log = get_logger(__name__)


class ToolRegistry:
    """Holds the tool allowlist and mediates every call."""

    def __init__(
        self,
        tools: list[Tool] | None = None,
        settings: Settings | None = None,
        cache: RedisCache | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._tools: dict[str, Tool] = {}
        self._cache = cache
        for tool in tools or []:
            self.register(tool)

    # -------------------------------------------------------------- registry
    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool must declare a name")
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas_for(self, principal: Principal) -> list[ToolSchema]:
        """Only advertise tools this principal could actually use.

        Not a security control - step 2 is - but it stops the model proposing
        calls that are certain to be denied, which wastes a turn and confuses
        the plan.
        """
        return [
            tool.to_llm_schema()
            for tool in self._tools.values()
            if self._permitted(tool, principal)
        ]

    def _permitted(self, tool: Tool, principal: Principal) -> bool:
        if tool.required_permission is None:
            return True
        return principal.has_permission(tool.required_permission)

    # ------------------------------------------------------------------ call
    async def call(
        self,
        name: str,
        raw_arguments: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Authorise, validate and execute one tool call."""
        started = time.perf_counter()

        # 1. Allowlist -------------------------------------------------------
        tool = self._tools.get(name)
        if tool is None:
            log.warning("tool.unknown", tool=name, user_id=ctx.principal.user_id)
            await self._audit(
                ctx,
                name,
                ToolCallStatus.DENIED,
                raw_arguments,
                denial_reason="unknown_tool",
                latency_ms=0,
            )
            return ToolResult.failure(name, "tool_not_found", f"No such tool: {name}")

        # 2. Authorisation ---------------------------------------------------
        if not self._permitted(tool, ctx.principal):
            reason = f"missing permission {tool.required_permission}"
            log.warning(
                "tool.denied",
                tool=name,
                user_id=ctx.principal.user_id,
                required=tool.required_permission,
            )
            await self._audit(
                ctx,
                name,
                ToolCallStatus.DENIED,
                raw_arguments,
                denial_reason=reason,
                latency_ms=0,
            )
            return ToolResult.failure(name, "authorization_failed", f"Not permitted: {reason}")

        # 3. Argument validation ---------------------------------------------
        try:
            args = tool.validate_arguments(raw_arguments or {})
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            log.info("tool.invalid_arguments", tool=name, detail=detail)
            await self._audit(
                ctx,
                name,
                ToolCallStatus.DENIED,
                raw_arguments,
                denial_reason=f"invalid_arguments: {detail}"[:255],
                latency_ms=0,
            )
            return ToolResult.failure(name, "tool_input_invalid", f"Invalid arguments: {detail}")

        # 4. Sensitive-action gate -------------------------------------------
        needs_approval = (
            tool.sensitive or name in SENSITIVE_TOOLS
        ) and self._settings.agent_require_approval_for_sensitive
        if needs_approval and name not in ctx.approved_actions:
            log.info("tool.approval_required", tool=name, user_id=ctx.principal.user_id)
            await self._audit(
                ctx,
                name,
                ToolCallStatus.DENIED,
                args.model_dump(mode="json"),
                denial_reason="human_approval_required",
                latency_ms=0,
            )
            return ToolResult.failure(
                name, "approval_required", "This action requires human approval"
            )

        validated = args.model_dump(mode="json")

        # 5. Cache -----------------------------------------------------------
        cache_key: str | None = None
        if tool.cacheable and self._cache is not None:
            cache_key = CacheKey.build(f"tool:{name}", validated)
            cached = await self._cache.get(cache_key)
            if cached is not None:
                elapsed = int((time.perf_counter() - started) * 1000)
                log.info("tool.cache_hit", tool=name, latency_ms=elapsed)
                await self._audit(
                    ctx,
                    name,
                    ToolCallStatus.EXECUTED,
                    validated,
                    result_summary=cached.get("summary"),
                    result_count=cached.get("count", 0),
                    latency_ms=elapsed,
                    cache_hit=True,
                )
                return ToolResult(
                    tool=name,
                    ok=True,
                    summary=cached.get("summary", ""),
                    data=cached.get("data"),
                    # Evidence must be cached alongside the data. Without it a
                    # cache hit silently returned an answer with no citations,
                    # which the verifier then (correctly) refused to ground.
                    evidence=[
                        EvidenceItem.model_validate(item) for item in cached.get("evidence", [])
                    ],
                    count=cached.get("count", 0),
                    latency_ms=elapsed,
                    cache_hit=True,
                )

        # 6. Execute under a timeout -----------------------------------------
        try:
            async with asyncio.timeout(tool.timeout_seconds):
                result = await tool.run(args, ctx)
        except TimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            log.warning("tool.timeout", tool=name, timeout_s=tool.timeout_seconds)
            await self._audit(
                ctx,
                name,
                ToolCallStatus.TIMEOUT,
                validated,
                error_code="tool_timeout",
                latency_ms=elapsed,
            )
            return ToolResult.failure(
                name,
                "tool_timeout",
                f"{name} exceeded its {tool.timeout_seconds}s budget",
                latency_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            log.exception("tool.unhandled_error", tool=name, error=str(exc))
            await self._audit(
                ctx,
                name,
                ToolCallStatus.FAILED,
                validated,
                error_code="tool_error",
                error_message=str(exc)[:1000],
                latency_ms=elapsed,
            )
            return ToolResult.failure(name, "tool_error", str(exc)[:300], latency_ms=elapsed)

        result.latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)

        # 7. Cache write + audit ---------------------------------------------
        if result.ok and cache_key and self._cache is not None:
            await self._cache.set(
                cache_key,
                {
                    "summary": result.summary,
                    "data": result.data,
                    "count": result.count,
                    "evidence": [item.model_dump(mode="json") for item in result.evidence],
                },
                ttl=tool.cache_ttl_seconds,
            )

        await self._audit(
            ctx,
            name,
            ToolCallStatus.EXECUTED if result.ok else ToolCallStatus.FAILED,
            validated,
            result_summary=result.summary[:2000],
            result_count=result.count,
            error_code=result.error_code,
            error_message=result.error_message,
            latency_ms=result.latency_ms,
            retry_count=result.retry_count,
        )
        log.info(
            "tool.executed",
            tool=name,
            ok=result.ok,
            count=result.count,
            latency_ms=result.latency_ms,
            degraded=result.degraded,
            user_id=ctx.principal.user_id,
        )
        return result

    async def call_many(
        self, calls: list[tuple[str, dict[str, Any]]], ctx: ToolContext
    ) -> list[ToolResult]:
        """Run independent tool calls concurrently.

        This is where most of the wall-clock saving in a multi-tool answer
        comes from: a literature search and a trials lookup have no dependency
        on each other, so paying for both serially is pure waste.
        """
        if not calls:
            return []
        if len(calls) == 1:
            return [await self.call(calls[0][0], calls[0][1], ctx)]
        return list(await asyncio.gather(*(self.call(name, args, ctx) for name, args in calls)))

    # ----------------------------------------------------------------- audit
    async def _audit(
        self,
        ctx: ToolContext,
        tool_name: str,
        status: ToolCallStatus,
        arguments: dict[str, Any],
        *,
        result_summary: str | None = None,
        result_count: int | None = None,
        denial_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        latency_ms: int = 0,
        retry_count: int = 0,
        cache_hit: bool = False,
    ) -> None:
        """Write the audit row in its own transaction.

        Deliberately not on the caller's session: if the agent run later fails
        and rolls back, the record of what it *attempted* must survive. An
        audit trail that disappears exactly when something went wrong is not an
        audit trail.
        """
        try:
            async with session_scope() as session:
                session.add(
                    ToolAuditLog(
                        run_id=ctx.run_id,
                        user_id=uuid.UUID(ctx.principal.user_id),
                        tool_name=tool_name,
                        status=status,
                        arguments=arguments,
                        result_summary=result_summary,
                        result_count=result_count,
                        denial_reason=denial_reason,
                        error_code=error_code,
                        error_message=error_message,
                        latency_ms=latency_ms,
                        retry_count=retry_count,
                        cache_hit=cache_hit,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - audit must not break the call
            log.error("tool.audit_write_failed", tool=tool_name, error=str(exc))
