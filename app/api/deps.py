"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import CurrentPrincipal, DbSession
from app.auth.permissions import Principal
from app.core.config import Settings, get_settings
from app.core.ratelimit import RateLimiter
from app.core.redis import RedisCache
from app.services.agent_service import AgentService
from app.services.document_service import DocumentService
from app.services.research_service import ResearchService

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_cache() -> RedisCache:
    return RedisCache()


CacheDep = Annotated[RedisCache, Depends(get_cache)]


def get_rate_limiter() -> RateLimiter:
    return RateLimiter()


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def get_agent_service(session: DbSession) -> AgentService:
    return AgentService(session)


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


def get_document_service(session: DbSession) -> DocumentService:
    return DocumentService(session)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def get_research_service(session: DbSession) -> ResearchService:
    return ResearchService(session)


ResearchServiceDep = Annotated[ResearchService, Depends(get_research_service)]


async def enforce_rate_limit(
    request: Request,
    principal: CurrentPrincipal,
    limiter: RateLimiterDep,
) -> Principal:
    """Per-user API rate limit, keyed on the authenticated identity."""
    await limiter.check(principal.user_id, bucket="api")
    return principal


async def enforce_agent_rate_limit(
    request: Request,
    principal: CurrentPrincipal,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> Principal:
    """Tighter budget for agent runs.

    An agent run costs orders of magnitude more than a read: several model
    calls, several tool calls, possibly a retrieval loop. It gets its own,
    much smaller bucket.
    """
    await limiter.check(
        principal.user_id,
        bucket="agent",
        limit=settings.agent_rate_limit_requests,
        window=settings.agent_rate_limit_window_seconds,
    )
    return principal


RateLimitedPrincipal = Annotated[Principal, Depends(enforce_rate_limit)]
AgentRateLimitedPrincipal = Annotated[Principal, Depends(enforce_agent_rate_limit)]
