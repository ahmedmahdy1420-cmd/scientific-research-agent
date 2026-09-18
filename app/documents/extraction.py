"""PDF text extraction and metadata inference with PyMuPDF.

Two things worth flagging in review:

* **Validation happens on the bytes, not the filename.** A file called
  `paper.pdf` that does not start with `%PDF-` is rejected before PyMuPDF sees
  it. Content-Type headers and extensions are attacker-controlled.
* **Encrypted and absurdly large PDFs are refused**, because both are cheap
  ways to burn a worker.

Metadata comes from the PDF's own dictionary first (cheap, reliable when
present) and falls back to a small LLM extraction over the first page. That
fallback is exactly the kind of short, schema-constrained task the *fast* model
is for — see app/llm/router.py.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pymupdf  # PyMuPDF (the `fitz` alias is deprecated)

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.rag.chunking import PageText, clean_text

log = get_logger(__name__)

PDF_MAGIC = b"%PDF-"
MAX_PAGES = 400

_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


@dataclass(slots=True)
class ExtractedDocument:
    pages: list[PageText]
    page_count: int
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    publication_date: dt.date | None = None
    keywords: list[str] = field(default_factory=list)
    abstract: str | None = None
    journal: str | None = None
    research_area: str | None = None

    @property
    def full_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def first_page_text(self) -> str:
        return self.pages[0].text if self.pages else ""


def validate_pdf_bytes(data: bytes, max_bytes: int) -> None:
    """Reject anything that is not a plausible, in-budget PDF."""
    if not data:
        raise ValidationError("Uploaded file is empty")
    if len(data) > max_bytes:
        raise ValidationError(
            f"File exceeds the {max_bytes // (1024 * 1024)}MB limit",
            details={"size_bytes": len(data), "max_bytes": max_bytes},
        )
    if not data.startswith(PDF_MAGIC):
        raise ValidationError(
            "File does not look like a PDF (bad magic bytes)",
            details={"expected": "%PDF-"},
        )


def extract_pdf(data: bytes) -> ExtractedDocument:
    """Extract text and metadata from PDF bytes."""
    validate_pdf_bytes(data, max_bytes=200 * 1024 * 1024)

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValidationError(f"Could not open the PDF: {exc}") from exc

    with document:
        if document.is_encrypted and not document.authenticate(""):
            raise ValidationError("Encrypted PDFs are not supported")
        if document.page_count > MAX_PAGES:
            raise ValidationError(f"PDF has {document.page_count} pages; the limit is {MAX_PAGES}")

        pages: list[PageText] = []
        for index in range(document.page_count):
            page = document.load_page(index)
            raw = page.get_text("text") or ""
            cleaned = clean_text(raw)
            if cleaned:
                pages.append(PageText(page_number=index + 1, text=cleaned))

        meta = document.metadata or {}
        page_count = document.page_count

    if not pages:
        raise ValidationError(
            "No extractable text found. This looks like a scanned PDF; it would "
            "need OCR, which this pipeline does not perform."
        )

    extracted = ExtractedDocument(pages=pages, page_count=page_count)
    extracted.title = (meta.get("title") or "").strip() or None
    author_field = (meta.get("author") or "").strip()
    if author_field:
        extracted.authors = [a.strip() for a in re.split(r"[;,]", author_field) if a.strip()][:20]
    extracted.keywords = [k.strip() for k in (meta.get("keywords") or "").split(",") if k.strip()][
        :20
    ]

    first_page = extracted.first_page_text
    doi_match = _DOI.search(first_page)
    if doi_match:
        extracted.doi = doi_match.group(0).rstrip(".")

    extracted.publication_date = _parse_pdf_date(meta.get("creationDate")) or _year_from(first_page)
    extracted.abstract = _extract_abstract(first_page)
    extracted.title = extracted.title or _guess_title(first_page)

    log.info(
        "extraction.completed",
        pages=len(pages),
        chars=len(extracted.full_text),
        has_doi=bool(extracted.doi),
        has_title=bool(extracted.title),
    )
    return extracted


def _parse_pdf_date(raw: str | None) -> dt.date | None:
    """PDF dates look like D:20240115103000+01'00'."""
    if not raw:
        return None
    match = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _year_from(text: str) -> dt.date | None:
    match = _YEAR.search(text)
    if not match:
        return None
    year = int(match.group(0))
    if 1900 <= year <= dt.date.today().year + 1:
        return dt.date(year, 1, 1)
    return None


def _extract_abstract(text: str) -> str | None:
    match = re.search(
        r"abstract\s*[:\-]?\s*(.{80,2500}?)(?:\n\s*(?:introduction|1\.|keywords)\b|\Z)",
        text,
        re.I | re.S,
    )
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()[:2500]


def _guess_title(text: str) -> str | None:
    """First substantial line that is not boilerplate."""
    for line in text.split("\n"):
        candidate = line.strip()
        if 15 <= len(candidate) <= 250 and not candidate.lower().startswith(
            ("doi", "http", "arxiv", "downloaded", "page ", "abstract")
        ):
            return candidate
    return None
