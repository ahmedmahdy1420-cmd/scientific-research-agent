"""Deterministic offline LLM provider.

Why this exists
---------------
Three concrete reasons, all of which come up in production work:

1. **The test suite must not need an API key.** Unit and integration tests that
   depend on a paid, non-deterministic, rate-limited network service are tests
   that get skipped. Here the LLM is injected, so tests assert on the agent's
   *control flow* — which tools ran, whether authorisation held, whether
   ungrounded answers are caught — rather than on model prose.
2. **The demo runs anywhere.** `docker compose up` gives a working agent with
   no credentials.
3. **It is a hard floor for evaluation.** Any model that does not beat this
   extractive baseline is not earning its cost.

This is NOT a language model and does not pretend to be. It is a small set of
deterministic, rule-based routines:

* classification by keyword/entity matching;
* planning by category lookup;
* synthesis by *extractive* quoting — it only ever repeats sentences that are
  present in the supplied evidence, which means it is grounded by construction
  and never hallucinates a citation;
* verification by set arithmetic on citation ids;
* embeddings via a hashed bag-of-words with n-grams, L2-normalised, so cosine
  similarity still reflects real lexical overlap and pgvector search returns
  sensible results offline.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import time
from collections import Counter
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.errors import LLMOutputValidationError
from app.core.logging import get_logger
from app.llm.base import (
    EmbeddingResponse,
    LLMClient,
    LLMResponse,
    Message,
    ParsedResult,
    TaskKind,
    ToolCallResponse,
    ToolSchema,
    Usage,
)
from app.llm.cost import approx_tokens
from app.models.enums import QuestionCategory

log = get_logger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

MODEL_NAME = "deterministic-offline"

_STOPWORDS = frozenset(
    """a an and are as at be been by for from has have how in is it its of on or that the
    to was were what when where which who why with about into than then there these those
    this can could would should do does did not no yes please tell me my our your""".split()
)

# --- category keyword tables -------------------------------------------------
_TRIAL_WORDS = (
    "clinical trial",
    "trial",
    "phase 1",
    "phase 2",
    "phase 3",
    "phase",
    "enrolment",
    "enrollment",
    "recruiting",
    "nct",
    "registry",
)
_STRUCTURED_WORDS = (
    "experiment",
    "experiments",
    "compound",
    "assay",
    "ic50",
    "p-value",
    "measured",
    "result",
    "results",
    "internal data",
    "cell line",
)
_LITERATURE_WORDS = (
    "research",
    "paper",
    "papers",
    "literature",
    "study",
    "studies",
    "evidence",
    "publication",
    "biomarker",
    "review",
    "preprint",
    "published",
    "findings",
)
_COMPARISON_WORDS = (
    "compare",
    "comparison",
    "versus",
    " vs ",
    "difference",
    "differences",
    "reconcile",
    "contrast",
    "both",
    "conclusions",
)
_SENSITIVE_WORDS = (
    "delete",
    "remove",
    "archive",
    "publish",
    "submit",
    "share externally",
    "export",
    "export to",
    "send to",
    "overwrite",
    "retract",
    "modify",
    "update the database",
    "drop table",
    "purge",
    "wipe",
)
_SMALLTALK_WORDS = (
    "hello",
    "hi there",
    "who are you",
    "what can you do",
    "what can you help",
    "how can you help",
    "what do you do",
    "your capabilities",
    "what are you able to",
    "thanks",
    "thank you",
)

_ENTITY_PATTERNS = [
    re.compile(r"\b(?:CMP|EXP|NCT)[-_]?\d{2,}\b", re.I),  # our codes
    re.compile(r"\b[A-Z]{2,4}-\d{2,4}\b"),  # generic compound codes
]
_DISEASE_WORDS = (
    "breast cancer",
    "lung cancer",
    "colorectal cancer",
    "pancreatic cancer",
    "melanoma",
    "glioblastoma",
    "alzheimer",
    "parkinson",
    "type 2 diabetes",
    "rheumatoid arthritis",
    "multiple sclerosis",
    "hypertension",
    "asthma",
    "psoriasis",
    "crohn",
    "leukemia",
    "lymphoma",
    "osteoporosis",
    "copd",
    "nash",
)


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 30]


def _extract_block(messages: list[Message], marker: str) -> str:
    for msg in reversed(messages):
        if marker in msg.content:
            return msg.content
    return messages[-1].content if messages else ""


def _extract_question(messages: list[Message]) -> str:
    blob = _extract_block(messages, "USER QUESTION:")
    match = re.search(r"USER QUESTION:\s*(.+?)(?:\n\n|\Z)", blob, re.S)
    if match:
        return match.group(1).strip()
    for msg in reversed(messages):
        if msg.role == "user":
            return msg.content.strip()
    return ""


def _extract_evidence_items(messages: list[Message]) -> list[tuple[str, str, str]]:
    """Pull (source_id, title, body) triples out of the fenced evidence block.

    Evidence lines are rendered by app/rag/pipeline.py as:
        [source_id] title :: body
    """
    blob = _extract_block(messages, "EVIDENCE")
    items: list[tuple[str, str, str]] = []
    for raw in blob.splitlines():
        line = raw.strip()
        match = re.match(r"^\[([^\]]+)\]\s*(.*?)\s*::\s*(.+)$", line)
        if match:
            items.append((match.group(1), match.group(2), match.group(3)))
    return items


def _sensitive_action_for(lowered: str) -> str:
    """Map a request onto one of the tool's allowlisted actions.

    The tool's Pydantic schema accepts only these three values, so the planner
    has to choose one rather than echoing the user's sentence back.
    """
    if any(w in lowered for w in ("export", "download", "extract to", "dump")):
        return "export_dataset"
    if any(w in lowered for w in ("share", "send to", "publish", "external")):
        return "share_externally"
    return "archive_document"


def _sensitive_target_for(question: str, entities: list[str]) -> str:
    """Best-effort target: a quoted string, a known entity code, or the phrase
    following "document about"."""
    quoted = re.search(r"['\"\u201c\u2018]([^'\"\u201d\u2019]{3,120})", question)
    if quoted:
        return quoted.group(1).strip()
    about = re.search(
        r"(?:document|report|paper|dataset)s?\s+(?:about|on|titled|named)\s+(.{3,90})",
        question,
        re.I,
    )
    if about:
        return about.group(1).strip().rstrip(".?! ").split(" from ")[0].strip()
    if entities:
        return entities[0]
    return question[:80]


class DeterministicLLMClient(LLMClient):
    """Rule-based stand-in for a language model. Fully deterministic."""

    provider = "deterministic"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._handlers: dict[str, Any] = {
            "QuestionClassification": self._classify,
            "AgentPlan": self._plan,
            "ResearchAnswer": self._answer,
            "VerificationResult": self._verify,
            "JudgeVerdict": self._judge,
            "DocumentMetadata": self._document_metadata,
        }

    # ------------------------------------------------------------------ util
    def _usage(self, messages: list[Message], output: str, started: float) -> Usage:
        prompt = sum(approx_tokens(m.content) for m in messages)
        return Usage(
            model=MODEL_NAME,
            prompt_tokens=prompt,
            completion_tokens=approx_tokens(output),
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=0.0,  # offline provider is free; that is the point
        )

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        messages: list[Message],
        *,
        task: TaskKind = "reasoning",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        question = _extract_question(messages)
        evidence = _extract_evidence_items(messages)

        if evidence:
            body = " ".join(f"{title}: {text}" for _, title, text in evidence[:3])
            content = f"Based on the retrieved material: {body}"
        else:
            content = (
                "I search an internal scientific document corpus, an experiment and compound "
                "database, and a clinical-trials registry, and I cite a source for every claim. "
                f"I have no retrieved evidence for: {question[:180]}"
            )
        return LLMResponse(content=content, usage=self._usage(messages, content, started))

    # ----------------------------------------------------- structured output
    async def structured_output(
        self,
        messages: list[Message],
        response_model: type[TModel],
        *,
        task: TaskKind = "fast",
        temperature: float | None = None,
    ) -> ParsedResult[TModel]:
        started = time.perf_counter()
        handler = self._handlers.get(response_model.__name__)
        if handler is None:
            raise LLMOutputValidationError(
                f"Offline provider has no handler for {response_model.__name__}. "
                "Set OPENAI_API_KEY to use a real model, or add a handler.",
                details={"schema": response_model.__name__},
            )
        payload = handler(messages, response_model)
        parsed = response_model.model_validate(payload)
        usage = self._usage(messages, str(payload), started)
        log.info(
            "llm.call",
            op="structured_output",
            model=MODEL_NAME,
            schema=response_model.__name__,
            latency_ms=usage.latency_ms,
        )
        return ParsedResult(parsed=parsed, usage=usage)

    # ------------------------------------------------------------ tool calls
    async def generate_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        task: TaskKind = "reasoning",
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        """The offline provider never proposes free-form tool calls.

        The graph's planner path is the one under test; leaving this empty keeps
        the offline behaviour honest instead of inventing arguments.
        """
        started = time.perf_counter()
        content = "Offline provider does not propose free-form tool calls."
        return ToolCallResponse(
            content=content, tool_calls=[], usage=self._usage(messages, content, started)
        )

    # ------------------------------------------------------------- embedding
    async def embed(self, texts: list[str], *, dimensions: int | None = None) -> EmbeddingResponse:
        """Hashed bag-of-words embedding with unigrams + bigrams, L2-normalised.

        Cosine similarity between two of these tracks lexical overlap, so
        pgvector retrieval returns genuinely relevant chunks offline. It has no
        semantic generalisation — "neoplasm" will not match "cancer" — which is
        exactly the gap a real embedding model fills.
        """
        started = time.perf_counter()
        dims = dimensions or self._settings.embedding_dim
        vectors = [self._embed_one(text, dims) for text in texts]
        usage = Usage(
            model=MODEL_NAME,
            prompt_tokens=sum(approx_tokens(t) for t in texts),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return EmbeddingResponse(vectors=vectors, usage=usage, model=MODEL_NAME, dimensions=dims)

    @staticmethod
    def _embed_one(text: str, dims: int) -> list[float]:
        vector = [0.0] * dims
        words = _tokens(text)
        if not words:
            vector[0] = 1.0
            return vector

        features = Counter(words)
        for first, second in itertools.pairwise(words):
            features[f"{first}_{second}"] += 1

        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]

    # =========================================================== handlers ===
    def _classify(self, messages: list[Message], _model: type[BaseModel]) -> dict[str, Any]:
        question = _extract_question(messages)
        lowered = f" {question.lower()} "

        entities: list[str] = []
        for pattern in _ENTITY_PATTERNS:
            entities.extend(m.group(0).upper() for m in pattern.finditer(question))
        entities.extend(d for d in _DISEASE_WORDS if d in lowered)

        def hits(words: tuple[str, ...]) -> int:
            return sum(1 for w in words if w in lowered)

        sensitive = hits(_SENSITIVE_WORDS) > 0
        if sensitive:
            return {
                "category": QuestionCategory.SENSITIVE_ACTION,
                "reasoning": "The request asks to modify, remove or externally share data.",
                "requires_tools": True,
                "is_sensitive": True,
                "entities": sorted(set(entities)),
            }

        scores = {
            QuestionCategory.COMPARISON: hits(_COMPARISON_WORDS) * 2,
            QuestionCategory.CLINICAL_TRIALS: hits(_TRIAL_WORDS) * 2,
            QuestionCategory.STRUCTURED_DATA: hits(_STRUCTURED_WORDS) * 2,
            QuestionCategory.LITERATURE: hits(_LITERATURE_WORDS),
            QuestionCategory.SMALL_TALK: hits(_SMALLTALK_WORDS) * 3,
        }
        category = max(scores, key=lambda k: scores[k])
        if scores[category] == 0:
            category = QuestionCategory.LITERATURE

        return {
            "category": category,
            "reasoning": f"Keyword match selected {category}.",
            "requires_tools": category
            not in (QuestionCategory.SMALL_TALK, QuestionCategory.OUT_OF_SCOPE),
            "is_sensitive": False,
            "entities": sorted(set(entities)),
        }

    def _plan(self, messages: list[Message], _model: type[BaseModel]) -> dict[str, Any]:
        question = _extract_question(messages)
        lowered = f" {question.lower()} "
        blob = _extract_block(messages, "CATEGORY:")
        cat_match = re.search(r"CATEGORY:\s*(\w+)", blob)
        category = cat_match.group(1) if cat_match else QuestionCategory.LITERATURE

        entities: list[str] = []
        for pattern in _ENTITY_PATTERNS:
            entities.extend(m.group(0).upper() for m in pattern.finditer(question))
        condition = next((d for d in _DISEASE_WORDS if d in lowered), question[:80])

        steps: list[dict[str, Any]] = []
        if category == QuestionCategory.SENSITIVE_ACTION:
            steps.append(
                {
                    "tool": "request_sensitive_action",
                    "rationale": "The request would change or externally share data.",
                    "arguments": {
                        "action": _sensitive_action_for(lowered),
                        "target": _sensitive_target_for(question, entities),
                        "justification": "Requested by the user via the research assistant.",
                    },
                }
            )
        elif category == QuestionCategory.CLINICAL_TRIALS:
            steps.append(
                {
                    "tool": "search_clinical_trials",
                    "rationale": "The question is about clinical trials.",
                    "arguments": {"condition": condition, "limit": 10},
                }
            )
            steps.append(
                {
                    "tool": "retrieve_documents",
                    "rationale": "Supporting literature for context.",
                    "arguments": {"query": question[:200], "limit": 5},
                }
            )
        elif category == QuestionCategory.STRUCTURED_DATA:
            target = entities[0] if entities else condition
            steps.append(
                {
                    "tool": "search_experiments_by_compound",
                    "rationale": "The question asks about internal experiments for a compound.",
                    "arguments": {"compound_query": target, "limit": 10},
                }
            )
        elif category == QuestionCategory.COMPARISON:
            steps.append(
                {
                    "tool": "retrieve_documents",
                    "rationale": "Published findings for the first side of the comparison.",
                    "arguments": {"query": question[:200], "limit": 8},
                }
            )
            steps.append(
                {
                    "tool": "search_experiments_by_compound",
                    "rationale": "Internal experimental findings to compare against.",
                    "arguments": {
                        "compound_query": entities[0] if entities else condition,
                        "limit": 5,
                    },
                }
            )
        elif category == QuestionCategory.SMALL_TALK:
            pass
        else:
            steps.append(
                {
                    "tool": "retrieve_documents",
                    "rationale": "The question asks what the literature says.",
                    "arguments": {"query": question[:200], "limit": 8},
                }
            )

        return {
            "objective": question[:300] or "Answer the user's research question",
            "steps": steps,
            "needs_human_approval": category == QuestionCategory.SENSITIVE_ACTION,
            "expected_evidence": "Passages or records that directly address the question.",
        }

    def _answer(self, messages: list[Message], _model: type[BaseModel]) -> dict[str, Any]:
        """Extractive synthesis: quote the evidence, cite it, claim nothing more."""
        question = _extract_question(messages)
        evidence = _extract_evidence_items(messages)

        if not evidence:
            return {
                "answer": (
                    "I could not find supporting material for this question in the indexed "
                    "corpus, so I will not attempt an answer."
                ),
                "citations": [],
                "confidence": "low",
                "caveats": ["No evidence was retrieved."],
                "unanswered_aspects": [question[:200]] if question else [],
            }

        query_terms = set(_tokens(question))
        scored: list[tuple[float, str, str, str]] = []
        for source_id, title, text in evidence:
            overlap = len(query_terms & set(_tokens(f"{title} {text}")))
            scored.append((overlap, source_id, title, text))
        scored.sort(key=lambda row: row[0], reverse=True)
        top = scored[:4]

        lines: list[str] = []
        citations: list[str] = []
        for _score, source_id, title, text in top:
            sentences = _sentences(text) or [text]
            best = max(sentences, key=lambda s: len(query_terms & set(_tokens(s))))
            lines.append(f"{title.strip()} reports: {best.strip()} [{source_id}]")
            citations.append(source_id)

        answer = (
            f"Across {len(top)} retrieved source(s) relevant to this question:\n\n"
            + "\n\n".join(f"- {line}" for line in lines)
            + "\n\nThese passages are quoted from the retrieved records; no claim above "
            "goes beyond them."
        )
        strongest = top[0][0] if top else 0
        return {
            "answer": answer,
            "citations": citations,
            "confidence": "medium" if strongest >= 3 else "low",
            "caveats": [
                "Produced by the offline extractive provider: it quotes retrieved text "
                "rather than reasoning over it. Set OPENAI_API_KEY for model synthesis."
            ],
            "unanswered_aspects": [],
        }

    def _verify(self, messages: list[Message], _model: type[BaseModel]) -> dict[str, Any]:
        """Set arithmetic: are the cited ids actually in the evidence?"""
        evidence = _extract_evidence_items(messages)
        valid_ids = {source_id for source_id, _, _ in evidence}

        blob = _extract_block(messages, "DRAFT ANSWER")
        cited = set(re.findall(r"\[([A-Za-z0-9:_\-\.]+)\]", blob))
        invalid = sorted(cited - valid_ids) if valid_ids else sorted(cited)

        grounded = not invalid
        sufficient = len(evidence) >= 2
        if not evidence:
            recommendation = "retrieve_more"
        elif invalid:
            recommendation = "refuse"
        elif not sufficient:
            recommendation = "answer_with_caveats"
        else:
            recommendation = "accept"

        return {
            "grounded": grounded,
            "sufficient": sufficient,
            "unsupported_claims": [],
            "invalid_citations": invalid,
            "missing_information": None if sufficient else "Only one source was retrieved.",
            "recommendation": recommendation,
            "reasoning": (
                f"Checked {len(cited)} citation(s) against {len(valid_ids)} evidence id(s); "
                f"{len(invalid)} did not resolve."
            ),
        }

    def _judge(self, messages: list[Message], _model: type[BaseModel]) -> dict[str, Any]:
        """Offline grading proxy: lexical overlap and citation presence only."""
        blob = _extract_block(messages, "ANSWER UNDER TEST")
        answer = blob
        has_citation = bool(re.search(r"\[[A-Za-z0-9:_\-\.]+\]", answer))
        expected = re.search(r"EXPECTED BEHAVIOUR:\s*(.+?)(?:\n[A-Z ]+:|\Z)", blob, re.S)
        expected_terms = set(_tokens(expected.group(1))) if expected else set()
        answer_terms = set(_tokens(answer))
        overlap = (
            len(expected_terms & answer_terms) / len(expected_terms) if expected_terms else 0.5
        )
        base = min(1.0, 0.35 + overlap)
        return {
            "grounding": 1.0 if has_citation else 0.3,
            "relevance": round(base, 3),
            "completeness": round(base * 0.9, 3),
            "citation_quality": 1.0 if has_citation else 0.0,
            "scope_adherence": 0.9,
            "reasoning": (
                "Offline judge: lexical-overlap proxy, not a language model. "
                f"citation_present={has_citation}, expected_term_overlap={overlap:.2f}. "
                "Treat as a smoke test, not a quality measurement."
            ),
        }

    def _document_metadata(
        self, messages: list[Message], _model: type[BaseModel]
    ) -> dict[str, Any]:
        """Regex metadata extraction from the first page of a PDF."""
        text = messages[-1].content if messages else ""
        title_match = re.search(r"(?:^|\n)\s*(?:TITLE:)?\s*([A-Z][^\n]{15,180})", text)
        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", text)
        year_match = re.search(r"\b(19|20)\d{2}\b", text)
        authors_match = re.search(r"Authors?:\s*(.+)", text)
        area_match = re.search(r"Research area:\s*(.+)", text)
        keywords_match = re.search(r"Keywords?:\s*(.+)", text)
        return {
            "title": (title_match.group(1).strip() if title_match else "Untitled document")[:300],
            "authors": (
                [a.strip() for a in authors_match.group(1).split(",")][:12] if authors_match else []
            ),
            "doi": doi_match.group(0) if doi_match else None,
            "publication_year": int(year_match.group(0)) if year_match else None,
            "research_area": area_match.group(1).strip()[:120] if area_match else None,
            "keywords": (
                [k.strip() for k in keywords_match.group(1).split(",")][:12]
                if keywords_match
                else []
            ),
            "abstract": None,
        }
