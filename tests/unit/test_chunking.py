"""Text cleaning and chunking."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.rag.chunking import PageText, chunk_pages, chunk_text, clean_text, detect_section

pytestmark = pytest.mark.unit


class TestCleaning:
    def test_hyphenated_line_break_is_rejoined(self):
        # PDF extraction splits words at line ends; a chunk containing
        # "bio- marker" would never match a query for "biomarker".
        assert "biomarker" in clean_text("The bio-\nmarker was elevated.")

    def test_running_headers_removed(self):
        cleaned = clean_text("Page 3 of 12\nReal content follows here in a sentence.")
        assert "Page 3 of 12" not in cleaned
        assert "Real content" in cleaned

    def test_paragraph_breaks_preserved_but_wraps_joined(self):
        cleaned = clean_text("First line\nwrapped here.\n\nSecond paragraph.")
        assert "First line wrapped here." in cleaned
        assert "\n\n" in cleaned

    def test_excess_whitespace_collapsed(self):
        assert clean_text("a     b\t\tc") == "a b c"


class TestSectionDetection:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Results", "Results"),
            ("  3. Discussion  ", "Discussion"),
            ("METHODS", "Methods"),
            ("Materials and Methods", "Materials And Methods"),
            ("The results were significant", None),
            ("", None),
        ],
    )
    def test_detect_section(self, line, expected):
        assert detect_section(line) == expected


class TestChunking:
    def test_chunks_carry_page_and_section(self):
        pages = [
            PageText(1, "Introduction\n\n" + "Background sentence. " * 40),
            PageText(2, "Results\n\n" + "We observed a significant effect. " * 40),
        ]
        chunks = chunk_pages(pages)
        assert chunks
        assert all(c.page_number in (1, 2) for c in chunks)
        assert any(c.section == "Results" for c in chunks)

    def test_indices_are_contiguous_from_zero(self):
        chunks = chunk_pages([PageText(1, "Sentence content here. " * 300)])
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_long_document_produces_multiple_chunks(self):
        chunks = chunk_pages([PageText(1, "A detailed sentence about biomarkers. " * 400)])
        assert len(chunks) > 1

    def test_overlap_preserves_boundary_spanning_facts(self):
        """A fact split across a chunk boundary must survive intact somewhere."""
        settings = get_settings()
        filler = "Filler sentence providing bulk to the document. " * 120
        needle = "The hazard ratio was zero point five eight."
        chunks = chunk_pages([PageText(1, f"{filler}\n\n{needle}\n\n{filler}")], settings)
        assert any(needle in c.content for c in chunks)

    def test_tiny_fragments_are_dropped(self):
        assert chunk_text("hi") == []

    def test_empty_input_is_safe(self):
        assert chunk_pages([]) == []
        assert chunk_text("") == []

    def test_token_counts_are_populated(self):
        chunks = chunk_text("Meaningful content about experimental results. " * 60)
        assert all(c.token_count > 0 for c in chunks)
