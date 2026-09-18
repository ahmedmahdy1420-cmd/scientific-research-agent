"""Optional reranking stage.

Bi-encoder retrieval (what pgvector does) is fast but approximate: it compares
a query vector to chunk vectors computed independently. A cross-encoder scores
the (query, chunk) *pair* and is markedly better at ordering, at the cost of a
network call per batch.

So reranking is optional and off by default (`RERANKER_PROVIDER=none`): the
project must run with no third-party keys. The interface exists, the Cohere
implementation is real, and the heuristic reranker gives a measurable ordering
improvement offline.
"""

from __future__ import annotations

import re
import time
from typing import ClassVar, Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas.search import RetrievedChunk

log = get_logger(__name__)


class Reranker(Protocol):
    name: str

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]: ...


class NoOpReranker:
    """Keeps vector order. The default."""

    name = "none"

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        return chunks[:top_k]


class HeuristicReranker:
    """Lexical-overlap reranker: no network, no key, measurably better than raw
    vector order when the query contains specific terms (compound codes, gene
    names) that a dense embedding blurs away.

    Score = 0.7 * vector similarity + 0.3 * query-term coverage, with a small
    bonus for chunks from a Results/Conclusion section, which is where findings
    actually live in a paper.
    """

    name = "heuristic"
    _SECTION_BONUS: ClassVar[dict[str, float]] = {
        "results": 0.05,
        "conclusion": 0.05,
        "conclusions": 0.05,
        "abstract": 0.03,
    }

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        terms = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
        if not terms:
            return chunks[:top_k]

        for chunk in chunks:
            words = set(re.findall(r"[a-z0-9]{3,}", chunk.content.lower()))
            coverage = len(terms & words) / len(terms)
            bonus = self._SECTION_BONUS.get((chunk.section or "").lower(), 0.0)
            chunk.rerank_score = round(0.7 * chunk.similarity + 0.3 * coverage + bonus, 6)

        return sorted(chunks, key=lambda c: c.rerank_score or 0.0, reverse=True)[:top_k]


class CohereReranker:
    """Cross-encoder reranking via Cohere's rerank endpoint.

    Fails *open*: if the call errors or times out we log and fall back to vector
    order. A reranker outage should degrade answer quality slightly, never take
    retrieval down.
    """

    name = "cohere"
    _URL = "https://api.cohere.com/v2/rerank"

    def __init__(self, settings: Settings | None = None, model: str = "rerank-v3.5") -> None:
        self._settings = settings or get_settings()
        self._model = model

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        if not self._settings.cohere_api_key or not chunks:
            return chunks[:top_k]

        started = time.perf_counter()
        payload = {
            "model": self._model,
            "query": query,
            "documents": [c.content[:4000] for c in chunks],
            "top_n": min(top_k, len(chunks)),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._settings.cohere_api_key}"},
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("rerank.failed_open", provider="cohere", error=str(exc))
            return chunks[:top_k]

        ordered: list[RetrievedChunk] = []
        for result in data.get("results", []):
            idx = result.get("index")
            if idx is None or idx >= len(chunks):
                continue
            chunk = chunks[idx]
            chunk.rerank_score = float(result.get("relevance_score", 0.0))
            ordered.append(chunk)

        log.info(
            "rerank.done",
            provider="cohere",
            latency_ms=int((time.perf_counter() - started) * 1000),
            kept=len(ordered),
        )
        return ordered or chunks[:top_k]


def build_reranker(settings: Settings | None = None) -> Reranker:
    cfg = settings or get_settings()
    if cfg.reranker_provider == "cohere" and cfg.cohere_api_key:
        return CohereReranker(cfg)
    if cfg.reranker_provider == "cohere":
        log.warning("rerank.cohere_without_key", falling_back_to="heuristic")
    return HeuristicReranker()
