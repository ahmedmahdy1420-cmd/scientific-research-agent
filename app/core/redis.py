"""Redis abstraction.

One shared async client, plus a small typed cache facade. Cache keys are
namespaced and *always* include an authorisation fingerprint when the value
could depend on who asked - see `CacheKey.for_user`. Caching a literature
search globally is fine (it is a pure function of the query); caching a
document list is not, because visibility differs per user.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_client: aioredis.Redis | None = None


def get_redis(settings: Settings | None = None) -> aioredis.Redis:
    """Process-wide async Redis client (lazily created, connection-pooled)."""
    global _client
    if _client is None:
        cfg = settings or get_settings()
        _client = aioredis.from_url(
            str(cfg.redis_url),
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def stable_hash(payload: Any) -> str:
    """Deterministic short hash of any JSON-serialisable payload."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


class CacheKey:
    """Namespaced cache-key construction."""

    PREFIX = "sra"

    @classmethod
    def build(cls, namespace: str, payload: Any) -> str:
        return f"{cls.PREFIX}:{namespace}:{stable_hash(payload)}"

    @classmethod
    def for_user(cls, namespace: str, user_id: str, payload: Any) -> str:
        """Key for a value whose contents depend on the caller's permissions."""
        return f"{cls.PREFIX}:{namespace}:u{user_id}:{stable_hash(payload)}"


class RedisCache:
    """Small JSON cache with TTL. Never fails a request because Redis is down."""

    def __init__(self, client: aioredis.Redis | None = None, *, ttl: int | None = None) -> None:
        cfg = get_settings()
        self._client = client or get_redis(cfg)
        self._ttl = ttl if ttl is not None else cfg.cache_ttl_seconds

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(key)
        except Exception as exc:  # noqa: BLE001 - cache is best effort
            log.warning("cache.get_failed", key=key, error=str(exc))
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            await self.delete(key)
            return None

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        try:
            await self._client.set(key, json.dumps(value, default=str), ex=ttl or self._ttl)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache.set_failed", key=key, error=str(exc))

    async def delete(self, *keys: str) -> None:
        try:
            if keys:
                await self._client.delete(*keys)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache.delete_failed", error=str(exc))

    async def incr_with_ttl(self, key: str, ttl: int) -> int:
        """Atomic counter used by the rate limiter."""
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        result = await pipe.execute()
        return int(result[0])
