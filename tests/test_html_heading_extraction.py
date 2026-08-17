"""Unit tests for HTML h1-h3 heading context extraction in extract_html()."""

from __future__ import annotations

import os

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")

import pytest

from nce.extractors.plaintext import _html_sections_from_headings, extract_html


class TestHtmlSectionsFromHeadings:
    def test_flat_html_returns_single_section(self):
        html = "<html><body><p>Hello world</p></body></html>"
        sections = _html_sections_from_headings(html)
        assert len(sections) >= 1
        assert any("Hello world" in s.text for s in sections)

    def test_h1_sets_structure_path(self):
        html = "<h1>Overview</h1><p>Intro text</p>"
        sections = _html_sections_from_headings(html)
        assert any(s.structure_path == "Overview" for s in sections)

    def test_h2_under_h1_builds_hierarchy(self):
        html = "<h1>Guide</h1><h2>Setup</h2><p>Install steps here</p>"
        sections = _html_sections_from_headings(html)
        paths = [s.structure_path for s in sections]
        assert any("Guide / Setup" in p for p in paths)

    def test_h3_under_h2_three_levels(self):
        html = "<h1>A</h1><h2>B</h2><h3>C</h3><p>Deep content</p>"
        sections = _html_sections_from_headings(html)
        paths = [s.structure_path for s in sections]
        assert any("A / B / C" in p for p in paths)

    def test_h2_resets_h3_context(self):
        html = (
            "<h1>Root</h1>"
            "<h2>First</h2><h3>Sub</h3><p>Sub text</p>"
            "<h2>Second</h2><p>Second text</p>"
        )
        sections = _html_sections_from_headings(html)
        paths = [s.structure_path for s in sections]
        assert any("Root / Second" == p for p in paths)

    def test_empty_html_returns_no_crash(self):
        sections = _html_sections_from_headings("")
        assert isinstance(sections, list)

    def test_section_order_is_sequential(self):
        html = "<h1>A</h1><p>text1</p><h2>B</h2><p>text2</p>"
        sections = _html_sections_from_headings(html)
        orders = [s.order for s in sections]
        assert orders == list(range(len(orders)))

    def test_headings_without_following_content_not_emitted(self):
        html = "<h1>Title Only</h1>"
        sections = _html_sections_from_headings(html)
        # No paragraph content — sections list may be empty or contain only
        # content nodes, not standalone heading-only sections
        for s in sections:
            assert s.text.strip() != ""


class TestExtractHtml:
    @pytest.mark.asyncio
    async def test_returns_extraction_result(self):
        blob = b"<h1>Hello</h1><p>World</p>"
        result = await extract_html(blob)
        assert result.method == "selectolax"
        assert "World" in result.text

    @pytest.mark.asyncio
    async def test_full_text_combines_all_sections(self):
        blob = b"<h1>A</h1><p>alpha</p><h2>B</h2><p>beta</p>"
        result = await extract_html(blob)
        assert "alpha" in result.text
        assert "beta" in result.text

    @pytest.mark.asyncio
    async def test_sections_carry_structure_path(self):
        blob = b"<h1>Guide</h1><h2>Install</h2><p>Run pip install.</p>"
        result = await extract_html(blob)
        paths = [s.structure_path for s in result.sections]
        assert any("Guide / Install" in p for p in paths)
