"""Redis fixed-window rate limiting.

Applied per authenticated user (falling back to client IP for anonymous
routes). Agent runs get their own, much tighter budget than plain reads
because each one can fan out into several LLM and tool calls.
"""

from __future__ import annotations

import time

from app.core.config import Settings, get_settings
from app.core.errors import RateLimitedError
from app.core.logging import get_logger
from app.core.redis import RedisCache

log = get_logger(__name__)


class RateLimiter:
    def __init__(self, cache: RedisCache | None = None, settings: Settings | None = None) -> None:
        self._cache = cache or RedisCache()
        self._settings = settings or get_settings()

    async def check(
        self,
        identity: str,
        *,
        bucket: str = "api",
        limit: int | None = None,
        window: int | None = None,
    ) -> None:
        """Raise RateLimitedError when `identity` exceeded its budget."""
        cfg = self._settings
        if not cfg.rate_limit_enabled:
            return

        limit = limit or cfg.rate_limit_requests
        window = window or cfg.rate_limit_window_seconds
        slot = int(time.time()) // window
        key = f"sra:ratelimit:{bucket}:{identity}:{slot}"

        try:
            count = await self._cache.incr_with_ttl(key, window + 1)
        except Exception as exc:  # noqa: BLE001
            # Fail *open* for availability: a Redis outage must not take the API
            # down. Trade-off is documented in README > Security model.
            log.warning("ratelimit.unavailable", error=str(exc), bucket=bucket)
            return

        if count > limit:
            retry_after = window - (int(time.time()) % window)
            log.warning("ratelimit.exceeded", identity=identity, bucket=bucket, count=count)
            raise RateLimitedError(
                f"Rate limit exceeded for {bucket}: {limit} requests per {window}s",
                details={"retry_after_seconds": retry_after, "limit": limit},
            )
