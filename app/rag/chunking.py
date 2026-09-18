"""Text cleaning and chunking.

Chunking is the highest-leverage, least-glamorous part of a RAG system. The
approach here is paragraph-aware with a token overlap:

* split on blank lines so a chunk rarely begins mid-sentence;
* pack paragraphs up to the target size, then start a new chunk;
* carry a fixed overlap of trailing words so a fact spanning a boundary is
  still fully present in one chunk;
* track the section heading and page number a chunk came from, which is what
  makes a citation point at "page 4, Results" instead of "document 7".

Token counts are approximated (~4 chars/token) rather than tokenised exactly:
the target is a soft budget, and an exact count is not worth the dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings

_WHITESPACE = re.compile(r"[ \t ]+")  # noqa: RUF001 - NBSP in the class is intentional
_MULTINEWLINE = re.compile(r"\n{3,}")
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_PAGE_NOISE = re.compile(
    r"^\s*(?:page\s+\d+(?:\s+of\s+\d+)?|\d+\s*/\s*\d+|downloaded from .*)\s*$",
    re.I | re.M,
)
_PARA = "\x00PARA\x00"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_SECTION = re.compile(
    r"^\s*(?:\d+\.?\s+)?(abstract|introduction|background|methods?|materials and methods|"
    r"results?|discussion|conclusions?|limitations?|references|acknowledge?ments?)\s*:?\s*$",
    re.I,
)


def clean_text(raw: str) -> str:
    """Normalise PDF-extracted text.

    Fixes the three things PyMuPDF output reliably suffers from: hyphenated
    line-wraps, running headers/footers, and ragged whitespace.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)  # "bio-\nmarker" -> "biomarker"
    text = _PAGE_NOISE.sub("", text)
    text = _WHITESPACE.sub(" ", text)
    text = _MULTINEWLINE.sub("\n\n", text)

    # Join lines that a PDF wrapped mid-sentence, but keep real paragraph
    # breaks. The paragraph marker is parked behind a sentinel first: the join
    # regex would otherwise consume the second newline of a "\n\n" pair and
    # silently destroy the paragraph structure the chunker depends on.
    text = text.replace("\n\n", _PARA)
    text = re.sub(r"(?<![.!?:;])\n(?![•\-\d])", " ", text)
    text = text.replace(_PARA, "\n\n")
    return text.strip()


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _split_oversized(paragraph: str, target: int) -> list[str]:
    """Split a paragraph that is bigger than one chunk.

    Necessary because cleaned PDF text regularly contains paragraphs that run
    for pages. Without it, such a document produced a single enormous chunk:
    too large to embed usefully, and useless for retrieval since the whole
    thing came back for any query matching one sentence inside it.

    Splits on sentence boundaries, falling back to a hard word split for the
    pathological case of a single sentence longer than the target.
    """
    if approx_tokens(paragraph) <= target:
        return [paragraph]

    pieces: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0

    for sentence in _SENTENCE_SPLIT.split(paragraph):
        if not sentence.strip():
            continue
        tokens = approx_tokens(sentence)

        if tokens > target:
            # A single sentence that alone exceeds the budget: split on words.
            if buffer:
                pieces.append(" ".join(buffer))
                buffer, buffer_tokens = [], 0
            words = sentence.split()
            step = max(1, target * 4 // 6)  # roughly `target` tokens of words
            pieces.extend(" ".join(words[i : i + step]) for i in range(0, len(words), step))
            continue

        if buffer and buffer_tokens + tokens > target:
            pieces.append(" ".join(buffer))
            buffer, buffer_tokens = [], 0
        buffer.append(sentence)
        buffer_tokens += tokens

    if buffer:
        pieces.append(" ".join(buffer))
    return [p for p in pieces if p.strip()]


@dataclass(slots=True)
class Chunk:
    index: int
    content: str
    token_count: int
    page_number: int | None = None
    section: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class PageText:
    page_number: int
    text: str


def detect_section(paragraph: str) -> str | None:
    match = _SECTION.match(paragraph.strip())
    return match.group(1).title() if match else None


def chunk_pages(
    pages: list[PageText],
    settings: Settings | None = None,
) -> list[Chunk]:
    """Chunk a document, preserving page numbers and section headings."""
    cfg = settings or get_settings()
    target = cfg.chunk_size_tokens
    overlap = cfg.chunk_overlap_tokens

    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_tokens = 0
    buffer_page: int | None = None
    current_section: str | None = None
    index = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens, index, buffer_page
        if not buffer:
            return
        content = "\n\n".join(buffer).strip()
        if len(content) < 40:  # drop scraps that cannot support a citation
            buffer, buffer_tokens = [], 0
            return
        chunks.append(
            Chunk(
                index=index,
                content=content,
                token_count=approx_tokens(content),
                page_number=buffer_page,
                section=current_section,
            )
        )
        index += 1
        # Carry the tail forward so a boundary-spanning fact stays intact.
        if overlap > 0:
            words = content.split()
            tail = words[-(overlap * 4 // 3) :] if len(words) > 10 else []
            buffer = [" ".join(tail)] if tail else []
            buffer_tokens = approx_tokens(buffer[0]) if buffer else 0
        else:
            buffer, buffer_tokens = [], 0

    for page in pages:
        for paragraph in (p.strip() for p in page.text.split("\n\n")):
            if not paragraph:
                continue
            heading = detect_section(paragraph)
            if heading:
                flush()
                current_section = heading
                continue
            if buffer_page is None or not buffer:
                buffer_page = page.page_number

            for piece in _split_oversized(paragraph, target):
                tokens = approx_tokens(piece)
                if buffer_tokens + tokens > target and buffer:
                    flush()
                    buffer_page = page.page_number
                buffer.append(piece)
                buffer_tokens += tokens

    flush()
    # Drop a trailing chunk that is nothing but carried-over overlap.
    if chunks and len(chunks) > 1 and chunks[-1].token_count < overlap:
        chunks.pop()
    return chunks


def chunk_text(text: str, settings: Settings | None = None) -> list[Chunk]:
    """Chunk a plain string (no page structure)."""
    return chunk_pages([PageText(page_number=1, text=clean_text(text))], settings)
