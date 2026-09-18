"""Vector retrieval against real pgvector."""

from __future__ import annotations

import pytest

from app.database.session import session_scope
from app.llm.factory import get_llm_client
from app.models.enums import AccessLevel, DocumentType, RoleName
from app.rag.embeddings import LLMEmbeddingProvider
from app.rag.pipeline import RAGPipeline, citation_id, preprocess_query
from app.rag.retriever import RetrievalFilters, Retriever
from tests.conftest import make_principal, requires_db

pytestmark = [pytest.mark.integration, requires_db]


@pytest.fixture
async def pipeline():
    async with session_scope() as session:
        embeddings = LLMEmbeddingProvider(get_llm_client())
        yield RAGPipeline(Retriever(session, embeddings))


class TestQueryPreprocessing:
    def test_interrogative_scaffolding_removed(self):
        cleaned = preprocess_query("Can you find recent research about HER2 biomarkers?")
        assert "?" not in cleaned
        assert "HER2" in cleaned

    def test_short_query_left_alone(self):
        assert preprocess_query("HER2") == "HER2"

    def test_never_returns_empty(self):
        assert preprocess_query("what is the?").strip()


class TestVectorSearch:
    async def test_relevant_chunk_ranked_first(self, pipeline, research_corpus):
        context = await pipeline.run(
            "PARP inhibition objective response rate in BRCA-deficient tumours",
            make_principal(RoleName.RESEARCHER),
        )
        assert context.evidence
        assert "PARP" in context.evidence[0].title

    async def test_similarity_is_higher_is_better_and_bounded(self, pipeline, research_corpus):
        context = await pipeline.run(
            "HER2 expression survival", make_principal(RoleName.RESEARCHER)
        )
        for chunk in context.chunks:
            assert -1.0 <= chunk.similarity <= 1.0
        similarities = [c.similarity for c in context.chunks]
        assert similarities == sorted(similarities, reverse=True) or context.reranked

    async def test_citation_ids_are_unique_and_stable(self, pipeline, research_corpus):
        context = await pipeline.run("breast cancer", make_principal(RoleName.RESEARCHER))
        ids = [e.source_id for e in context.evidence]
        assert len(ids) == len(set(ids))
        assert all(i.startswith("doc:") for i in ids)

    async def test_evidence_ids_match_the_valid_set(self, pipeline, research_corpus):
        context = await pipeline.run("HER2", make_principal(RoleName.RESEARCHER))
        assert {e.source_id for e in context.evidence} == context.valid_citation_ids

    async def test_empty_corpus_query_returns_nothing_gracefully(self, pipeline, research_corpus):
        context = await pipeline.run(
            "zzzz completely unrelated volcanology terminology zzzz",
            make_principal(RoleName.RESEARCHER),
            top_k=3,
        )
        assert isinstance(context.evidence, list)


class TestMetadataFilters:
    async def test_research_area_filter(self, pipeline, research_corpus):
        context = await pipeline.run(
            "breast cancer",
            make_principal(RoleName.RESEARCHER),
            filters=RetrievalFilters(research_area="cardiology"),
        )
        assert context.evidence == []

    async def test_date_filter_excludes_older_documents(self, pipeline, research_corpus):
        import datetime as dt

        context = await pipeline.run(
            "PARP inhibition",
            make_principal(RoleName.RESEARCHER),
            filters=RetrievalFilters(date_from=dt.date(2026, 1, 1)),
        )
        # The PARP review is from 2024, so it must be filtered out.
        assert all("PARP" not in e.title for e in context.evidence)

    async def test_document_type_filter(self, pipeline, research_corpus):
        context = await pipeline.run(
            "breast cancer",
            make_principal(RoleName.RESEARCHER),
            filters=RetrievalFilters(document_types=[DocumentType.REVIEW]),
        )
        assert all("PARP" in e.title for e in context.evidence) or not context.evidence

    async def test_access_level_filter_cannot_be_overridden_by_the_caller(
        self, pipeline, research_corpus
    ):
        """Even if a filter object arrives asking for restricted content, the
        principal's clearance overwrites it."""
        context = await pipeline.run(
            "internal safety review adverse events",
            make_principal(RoleName.RESEARCHER),
            filters=RetrievalFilters(
                allowed_access_levels=[AccessLevel.RESTRICTED]  # attacker-supplied
            ),
        )
        blob = " ".join(e.snippet for e in context.evidence)
        assert "three unpublished" not in blob


class TestContextConstruction:
    async def test_evidence_block_is_fenced(self, pipeline, research_corpus):
        context = await pipeline.run("HER2", make_principal(RoleName.RESEARCHER))
        assert "QUOTED SOURCE MATERIAL" in context.evidence_block
        assert "<<<EVIDENCE:" in context.evidence_block

    async def test_one_document_cannot_dominate_the_context(self, pipeline, research_corpus):
        from collections import Counter

        from app.rag.pipeline import MAX_CHUNKS_PER_DOCUMENT

        context = await pipeline.run("breast cancer", make_principal(RoleName.RESEARCHER), top_k=20)
        counts = Counter(c.document_id for c in context.chunks)
        assert all(n <= MAX_CHUNKS_PER_DOCUMENT for n in counts.values())


class TestEmbeddingProvider:
    async def test_query_embedding_matches_the_column_width(self):
        from app.core.config import get_settings

        provider = LLMEmbeddingProvider(get_llm_client())
        vector = await provider.embed_query("breast cancer biomarkers")
        assert len(vector) == get_settings().embedding_dim

    async def test_empty_query_is_rejected(self):
        from app.core.errors import EmbeddingError

        provider = LLMEmbeddingProvider(get_llm_client())
        with pytest.raises(EmbeddingError):
            await provider.embed_query("   ")


def test_citation_id_format():
    import uuid

    value = citation_id(uuid.UUID("12345678-1234-5678-1234-567812345678"), 3)
    assert value == "doc:12345678:3"
