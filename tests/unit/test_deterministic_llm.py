"""The offline provider.

It is not a language model, but it is the default in tests and in a
credential-free demo, so its behaviour is part of the contract.
"""

from __future__ import annotations

import math

import pytest

from app.core.config import get_settings
from app.llm.base import Message
from app.models.enums import QuestionCategory
from app.schemas.agent import (
    AgentPlan,
    QuestionClassification,
    ResearchAnswer,
    VerificationResult,
)

pytestmark = pytest.mark.unit


def _messages(question: str, evidence: list[str] | None = None) -> list[Message]:
    from app.llm.prompts import build_evidence_block, build_user_question_block

    content = build_user_question_block(question)
    if evidence is not None:
        content += "\n\n" + build_evidence_block(evidence)
    return [Message(role="system", content="s"), Message(role="user", content=content)]


class TestEmbeddings:
    async def test_vectors_are_unit_length_and_correctly_sized(self, deterministic_llm):
        response = await deterministic_llm.embed(["breast cancer biomarker HER2"])
        vector = response.vectors[0]
        assert len(vector) == get_settings().embedding_dim
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)

    async def test_deterministic_across_calls(self, deterministic_llm):
        a = (await deterministic_llm.embed(["identical text"])).vectors[0]
        b = (await deterministic_llm.embed(["identical text"])).vectors[0]
        assert a == b

    async def test_similar_text_scores_higher_than_unrelated(self, deterministic_llm):
        vectors = (
            await deterministic_llm.embed(
                [
                    "breast cancer biomarker HER2 expression",
                    "breast cancer biomarker HER2 levels",
                    "volcanic activity in Iceland during winter",
                ]
            )
        ).vectors

        def cosine(x, y):
            return sum(a * b for a, b in zip(x, y, strict=True))

        assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2])

    async def test_empty_input_returns_empty(self, deterministic_llm):
        assert (await deterministic_llm.embed([])).vectors == []

    async def test_batch_size_is_preserved(self, deterministic_llm):
        response = await deterministic_llm.embed([f"text {i}" for i in range(70)])
        assert len(response.vectors) == 70


class TestClassification:
    @pytest.mark.parametrize(
        "question,expected",
        [
            ("Find clinical trials for melanoma in phase 3", QuestionCategory.CLINICAL_TRIALS),
            ("Which experiments used compound CMP-0004?", QuestionCategory.STRUCTURED_DATA),
            ("Compare these two research findings", QuestionCategory.COMPARISON),
            ("Delete the document about biomarkers", QuestionCategory.SENSITIVE_ACTION),
            ("What can you do?", QuestionCategory.SMALL_TALK),
        ],
    )
    async def test_categories(self, deterministic_llm, question, expected):
        result = await deterministic_llm.structured_output(
            _messages(question), QuestionClassification
        )
        assert result.parsed.category == expected

    async def test_sensitive_requests_are_flagged(self, deterministic_llm):
        result = await deterministic_llm.structured_output(
            _messages("Please export the dataset to an external partner"),
            QuestionClassification,
        )
        assert result.parsed.is_sensitive is True

    async def test_entities_are_extracted(self, deterministic_llm):
        result = await deterministic_llm.structured_output(
            _messages("Which experiments used CMP-0042 for breast cancer?"),
            QuestionClassification,
        )
        assert "CMP-0042" in result.parsed.entities
        assert "breast cancer" in result.parsed.entities


class TestPlanning:
    async def test_sensitive_plan_uses_an_allowlisted_action(self, deterministic_llm):
        messages = _messages("Delete the document about breast cancer biomarkers")
        messages[1].content += "\n\nCATEGORY: sensitive_action"
        plan = (await deterministic_llm.structured_output(messages, AgentPlan)).parsed
        assert plan.needs_human_approval
        step = plan.steps[0]
        assert step.tool == "request_sensitive_action"
        # Must be a schema-valid action, not the user's sentence.
        assert step.arguments["action"] in (
            "archive_document",
            "export_dataset",
            "share_externally",
        )

    async def test_literature_plan_uses_retrieval(self, deterministic_llm):
        messages = _messages("What does the literature say about PARP inhibitors?")
        messages[1].content += "\n\nCATEGORY: literature"
        plan = (await deterministic_llm.structured_output(messages, AgentPlan)).parsed
        assert plan.steps[0].tool == "retrieve_documents"
        assert not plan.needs_human_approval


class TestSynthesis:
    async def test_answer_is_extractive_and_cites_only_real_ids(self, deterministic_llm):
        evidence = [
            "[doc:aaa:0] HER2 study :: HER2-high status was associated with improved survival.",
            "[doc:bbb:1] PARP study :: PARP inhibition showed a 41% response rate.",
        ]
        answer = (
            await deterministic_llm.structured_output(
                _messages("What do we know about HER2?", evidence), ResearchAnswer
            )
        ).parsed
        assert answer.citations
        assert set(answer.citations) <= {"doc:aaa:0", "doc:bbb:1"}

    async def test_refuses_when_no_evidence(self, deterministic_llm):
        answer = (
            await deterministic_llm.structured_output(
                _messages("What about quantum biomarker resonance?", []), ResearchAnswer
            )
        ).parsed
        assert answer.citations == []
        assert answer.confidence == "low"
        assert "could not find" in answer.answer.lower()


class TestVerification:
    async def test_valid_citations_accepted(self, deterministic_llm):
        evidence = [
            "[doc:aaa:0] A :: finding one here in a full sentence.",
            "[doc:bbb:0] B :: finding two here in a full sentence.",
        ]
        messages = _messages("q", evidence)
        messages[
            1
        ].content += "\n\nDRAFT ANSWER:\nA reports x [doc:aaa:0] and B reports y [doc:bbb:0]."
        result = (await deterministic_llm.structured_output(messages, VerificationResult)).parsed
        assert result.grounded
        assert result.invalid_citations == []
        assert result.recommendation == "accept"

    async def test_fabricated_citation_is_caught(self, deterministic_llm):
        evidence = ["[doc:aaa:0] A :: a real finding in a complete sentence here."]
        messages = _messages("q", evidence)
        messages[1].content += "\n\nDRAFT ANSWER:\nAs shown in [doc:fake:9], the effect is large."
        result = (await deterministic_llm.structured_output(messages, VerificationResult)).parsed
        assert not result.grounded
        assert "doc:fake:9" in result.invalid_citations
        assert result.recommendation == "refuse"


class TestUnsupportedSchema:
    async def test_unknown_schema_raises_rather_than_guessing(self, deterministic_llm):
        from pydantic import BaseModel

        from app.core.errors import LLMOutputValidationError

        class Unknown(BaseModel):
            x: int

        with pytest.raises(LLMOutputValidationError, match="no handler"):
            await deterministic_llm.structured_output(_messages("q"), Unknown)


class TestToolCalling:
    async def test_offline_provider_proposes_no_tool_calls(self, deterministic_llm):
        response = await deterministic_llm.generate_with_tools(_messages("q"), [])
        assert response.tool_calls == []
