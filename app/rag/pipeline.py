"""End-to-end RAG pipeline.

    question
      -> query preprocessing
      -> embedding (cached)
      -> pgvector similarity search   (with the caller's access filter)
      -> metadata filtering
      -> top-K candidates
      -> optional reranking
      -> context construction (fenced, citable)
      -> LLM
      -> answer + citations

Two details that matter more than they look:

* **Stable citation ids.** Every passage gets `doc:<uuid8>:<chunk_index>`,
  which the model must echo verbatim. Because the id set is known, the
  verifier can check citations by set membership — a fabricated citation is
  caught deterministically rather than by asking a model to be careful.
* **Context is deduplicated and budgeted.** Several chunks from the same
  document are capped, so one verbose paper cannot crowd out the rest of the
  evidence.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from app.auth.permissions import Principal
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.llm.prompts import build_evidence_block
from app.rag.reranker import Reranker, build_reranker
from app.rag.retriever import RetrievalFilters, Retriever
from app.schemas.agent import EvidenceItem
from app.schemas.search import RetrievedChunk

log = get_logger(__name__)

_STOPWORDS = frozenset(
    "what which who why how find show tell me about please can you the a an of for on in to".split()
)
#: At most this many passages from any single document, so one source cannot
#: dominate the context window.
MAX_CHUNKS_PER_DOCUMENT = 3


def preprocess_query(question: str) -> str:
    """Turn a conversational question into a retrieval query.

    Deliberately conservative: strip the interrogative scaffolding and
    collapse whitespace. No LLM rewriting on the hot path — it adds a
    round-trip to every request and can silently drop the user's specific
    terms, which is the opposite of what retrieval needs.
    """
    text = question.strip()
    text = re.sub(r"^(please\s+)?(can you\s+|could you\s+)?", "", text, flags=re.I)
    text = re.sub(r"[?!]+$", "", text)
    words = [w for w in text.split() if w.lower().strip(",.") not in _STOPWORDS]
    cleaned = " ".join(words) if len(words) >= 3 else text
    return cleaned[:1000].strip() or question[:1000]


def citation_id(document_id: uuid.UUID, chunk_index: int) -> str:
    """Short, stable, model-echoable id for a passage."""
    return f"doc:{str(document_id)[:8]}:{chunk_index}"


@dataclass(slots=True)
class RAGContext:
    """Everything the synthesis step needs, and nothing it does not."""

    query: str
    chunks: list[RetrievedChunk]
    evidence: list[EvidenceItem]
    evidence_block: str
    valid_citation_ids: set[str] = field(default_factory=set)
    retrieval_latency_ms: int = 0
    reranked: bool = False
    filters: dict = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.evidence


class RAGPipeline:
    def __init__(
        self,
        retriever: Retriever,
        reranker: Reranker | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._retriever = retriever
        self._settings = settings or get_settings()
        self._reranker = reranker or build_reranker(self._settings)

    async def run(
        self,
        question: str,
        principal: Principal,
        *,
        filters: RetrievalFilters | None = None,
        top_k: int | None = None,
    ) -> RAGContext:
        cfg = self._settings
        query = preprocess_query(question)
        active_filters = filters or RetrievalFilters()

        candidates, latency = await self._retriever.retrieve_documents(
            query,
            principal,
            filters=active_filters,
            limit=cfg.rag_candidate_k,
        )

        k = top_k or cfg.rag_top_k
        reranked = False
        if candidates and self._reranker.name != "none":
            candidates = await self._reranker.rerank(query, candidates, k)
            reranked = True
        else:
            candidates = candidates[:k]

        selected = self._cap_per_document(candidates)
        evidence = [self._to_evidence(chunk) for chunk in selected]
        lines = [f"[{item.source_id}] {item.title} :: {item.snippet}" for item in evidence]

        context = RAGContext(
            query=query,
            chunks=selected,
            evidence=evidence,
            evidence_block=build_evidence_block(lines),
            valid_citation_ids={item.source_id for item in evidence},
            retrieval_latency_ms=latency,
            reranked=reranked,
            filters=active_filters.describe(),
        )
        log.info(
            "rag.context_built",
            query=query[:120],
            candidates=len(candidates),
            selected=len(selected),
            reranked=reranked,
            latency_ms=latency,
        )
        return context

    @staticmethod
    def _cap_per_document(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        seen: dict[uuid.UUID, int] = {}
        kept: list[RetrievedChunk] = []
        for chunk in chunks:
            count = seen.get(chunk.document_id, 0)
            if count >= MAX_CHUNKS_PER_DOCUMENT:
                continue
            seen[chunk.document_id] = count + 1
            kept.append(chunk)
        return kept

    @staticmethod
    def _to_evidence(chunk: RetrievedChunk) -> EvidenceItem:
        title = chunk.document_title
        if chunk.page_number:
            title = f"{title} (p.{chunk.page_number}"
            title += f", {chunk.section})" if chunk.section else ")"
        return EvidenceItem(
            source_type="document",
            source_id=citation_id(chunk.document_id, chunk.chunk_index),
            title=title,
            snippet=chunk.content.strip().replace("\n", " ")[:1200],
            origin_tool="retrieve_documents",
            relevance_score=round(chunk.rerank_score or chunk.similarity, 4),
            publication_date=chunk.publication_date,
            url=f"https://doi.org/{chunk.doi}" if chunk.doi else None,
        )
