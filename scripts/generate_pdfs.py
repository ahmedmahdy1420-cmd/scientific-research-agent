#!/usr/bin/env python
"""Render the synthetic documents to real PDFs.

The ingestion pipeline must be exercised on actual PDF bytes — PyMuPDF
extraction, page numbers, section detection and the cleaning rules only mean
anything against a real file. Writing the sample corpus straight into
`document_chunks` would skip the half of the pipeline most likely to break.

Run:  python -m scripts.generate_pdfs
Output: data/pdfs/*.pdf
"""

from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "sample" / "documents.json"
OUT = ROOT / "data" / "pdfs"

SECTION_HEADS = {
    "abstract",
    "introduction",
    "methods",
    "results",
    "discussion",
    "limitations",
    "conclusions",
    "references",
}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "DocTitle", parent=base["Title"], fontSize=15, leading=19, spaceAfter=10
        ),
        "meta": ParagraphStyle(
            "Meta", parent=base["Normal"], fontSize=8.5, leading=12, textColor="#444444"
        ),
        "heading": ParagraphStyle(
            "Heading",
            parent=base["Heading2"],
            fontSize=11.5,
            leading=15,
            spaceBefore=12,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["BodyText"], fontSize=9.6, leading=14, spaceAfter=7
        ),
    }


def render(document: dict, path: Path, styles: dict[str, ParagraphStyle]) -> None:
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=document["title"],
        author=", ".join(document.get("authors") or []),
        subject=document.get("research_area") or "",
        keywords=", ".join(document.get("keywords") or []),
    )

    flow = [Paragraph(document["title"], styles["title"])]

    meta_bits = [
        "Authors: " + ", ".join(document.get("authors") or ["Unknown"]),
        f"Research area: {document.get('research_area') or 'n/a'}",
        f"Keywords: {', '.join(document.get('keywords') or [])}",
    ]
    if document.get("journal"):
        meta_bits.insert(1, f"{document['journal']} ({document.get('publication_date', '')[:4]})")
    if document.get("doi"):
        meta_bits.append(f"DOI: {document['doi']}")
    meta_bits.append(
        "SYNTHETIC DOCUMENT - generated for demonstration. Not a real scientific source."
    )
    for bit in meta_bits:
        flow.append(Paragraph(bit, styles["meta"]))
    flow.append(Spacer(1, 7 * mm))

    for block in document["body"].split("\n\n"):
        text = block.strip()
        if not text:
            continue
        first_line = text.split("\n", 1)[0].strip()
        if first_line.lower().rstrip(":") in SECTION_HEADS and len(first_line) < 40:
            flow.append(Paragraph(first_line, styles["heading"]))
            remainder = text[len(first_line) :].strip()
            if remainder:
                flow.append(Paragraph(remainder.replace("\n", " "), styles["body"]))
        else:
            flow.append(Paragraph(text.replace("\n", " "), styles["body"]))

    doc.build(flow)


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"{SOURCE} not found. Run `python -m scripts.generate_sample_data` first.")
    OUT.mkdir(parents=True, exist_ok=True)
    styles = _styles()
    documents = json.loads(SOURCE.read_text())

    for document in documents:
        path = OUT / document["filename"]
        render(document, path, styles)

    total = sum(p.stat().st_size for p in OUT.glob("*.pdf"))
    print(f"wrote {len(documents)} PDFs to {OUT.relative_to(ROOT)} ({total // 1024} KB)")


if __name__ == "__main__":
    main()
