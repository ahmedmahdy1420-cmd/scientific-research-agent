"""Embedding generation and persistence.

The provider is swappable (`EmbeddingProvider`) because embeddings are the one
choice you are most likely to revisit: a better/cheaper model appears, or you
move to a self-hosted one for data-residency reasons. Re-embedding a corpus is
expensive, so `document_chunks.embedding_model` records which model produced
each vector and a migration can re-embed only what is stale.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import EmbeddingError
from app.core.logging import get_logger
from app.core.redis import CacheKey, RedisCache
from app.llm.base import LLMClient
from app.models.document import DocumentChunk

log = get_logger(__name__)


class EmbeddingProvider(Protocol):
    """Minimal surface the retriever and the ingestion worker depend on."""

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class LLMEmbeddingProvider:
    """Embedding provider backed by whatever `LLMClient` is configured."""

    def __init__(
        self,
        client: LLMClient,
        settings: Settings | None = None,
        cache: RedisCache | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or get_settings()
        self._cache = cache

    @property
    def model(self) -> str:
        if self._client.provider == "openai":
            return self._settings.embedding_model
        return "deterministic-offline"

    @property
    def dimensions(self) -> int:
        return self._settings.embedding_dim

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts, batched to the provider's limit.

        Batches are sent concurrently: embedding 400 chunks serially is minutes
        of avoidable wall-clock during ingestion.
        """
        if not texts:
            return []

        size = max(1, self._settings.embedding_batch_size)
        batches = [texts[i : i + size] for i in range(0, len(texts), size)]

        async def run(batch: list[str]) -> list[list[float]]:
            response = await self._client.embed(batch, dimensions=self.dimensions)
            if len(response.vectors) != len(batch):
                raise EmbeddingError(
                    "Embedding provider returned a different number of vectors than inputs",
                    details={"expected": len(batch), "received": len(response.vectors)},
                )
            return response.vectors

        results = await asyncio.gather(*(run(b) for b in batches))
        vectors = [vec for batch in results for vec in batch]

        for vec in vectors:
            if len(vec) != self.dimensions:
                raise EmbeddingError(
                    "Embedding width does not match the configured column width",
                    details={"expected": self.dimensions, "received": len(vec)},
                )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query, with a Redis cache.

        A query embedding is a pure function of (text, model, dims), so caching
        it is always safe — no authorisation context is involved.
        """
        cleaned = text.strip()
        if not cleaned:
            raise EmbeddingError("Cannot embed an empty query")

        key = CacheKey.build("embed:query", {"t": cleaned, "m": self.model, "d": self.dimensions})
        if self._cache is not None:
            cached = await self._cache.get(key)
            if isinstance(cached, list) and len(cached) == self.dimensions:
                return [float(v) for v in cached]

        vectors = await self.embed_texts([cleaned])
        vector = vectors[0]
        if self._cache is not None:
            await self._cache.set(key, vector, ttl=3600)
        return vector


async def generate_embedding(provider: EmbeddingProvider, text: str) -> list[float]:
    """Embed one piece of text (spec-named helper)."""
    return await provider.embed_query(text)


async def store_embedding(
    session: AsyncSession,
    chunk_id: uuid.UUID,
    vector: list[float],
    model: str,
) -> None:
    """Persist one vector against an existing chunk."""
    await session.execute(
        update(DocumentChunk)
        .where(DocumentChunk.id == chunk_id)
        .values(embedding=vector, embedding_model=model)
    )


async def store_embeddings_bulk(
    session: AsyncSession,
    pairs: list[tuple[uuid.UUID, list[float]]],
    model: str,
) -> int:
    """Persist many vectors. Called once per document by the ingestion task."""
    for chunk_id, vector in pairs:
        await store_embedding(session, chunk_id, vector, model)
    return len(pairs)


async def count_unembedded_chunks(session: AsyncSession) -> int:
    stmt = select(DocumentChunk.id).where(DocumentChunk.embedding.is_(None)).limit(1000)
    return len((await session.execute(stmt)).scalars().all())
