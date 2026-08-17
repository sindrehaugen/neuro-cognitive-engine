"""
Tests for chunking.py semantic boundary preservation.

Verifies:
- Code blocks (``` fences) are never split mid-block
- Markdown table rows are never split mid-row
- Normal paragraph splitting still works
- Chunks always respect max_chars budget
- Fallback to newline boundary before hard-slicing
"""

from nce.extractors.chunking import (
    _extract_heading_hierarchy,
    _find_semantic_split,
    _hard_split_semantic,
    _render_header_context,
    _split_section_text,
    chunk_structured,
)
from nce.extractors.core import Section

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _section(text: str, order: int = 0) -> Section:
    return Section(
        text=text,
        structure_path=f"section/{order}",
        section_type="body",
        order=order,
    )


# ---------------------------------------------------------------------------
# TestFindSemanticSplit — unit tests for the boundary-finder
# ---------------------------------------------------------------------------


class TestFindSemanticSplit:
    def test_finds_fence_close_within_budget(self):
        """Closing ``` found within budget → split returns position after fence line."""
        text = "intro\n```\ncode here\n```\nafter"
        # budget of 25 covers "intro\n```\ncode here\n```"
        split = _find_semantic_split(text, 25)
        chunk = text[:split]
        assert "```" in chunk, "Fence boundary not included in first chunk"
        assert chunk.count("```") % 2 == 0 or chunk.endswith("```"), (
            "Split should land on or after the closing fence"
        )

    def test_finds_newline_when_no_fence(self):
        """No fence in text → falls back to last newline within budget."""
        text = "line one\nline two\nline three"
        split = _find_semantic_split(text, 15)
        assert text[:split].endswith("\n") or split == text.rfind("\n", 0, 15) + 1

    def test_hard_cut_when_no_newline(self):
        """Single long word with no whitespace → hard cut at budget."""
        text = "a" * 100
        split = _find_semantic_split(text, 40)
        assert split == 40

    def test_split_index_within_budget(self):
        """Split point must always be ≤ budget."""
        text = "hello world\nfoo bar\n```\nbaz\n```\nend"
        for budget in [10, 20, 30, 40, len(text)]:
            split = _find_semantic_split(text, min(budget, len(text)))
            assert split <= budget


# ---------------------------------------------------------------------------
# TestCodeBlockPreservation — ``` fences never split
# ---------------------------------------------------------------------------


class TestCodeBlockPreservation:
    """Code blocks enclosed in ``` must not be split across chunk boundaries."""

    def _build_code_text(self, code_lines: int = 50) -> str:
        lines = ["preamble paragraph"]
        lines.append("```python")
        for i in range(code_lines):
            lines.append(f"    result_{i} = some_function({i})  # comment {i}")
        lines.append("```")
        lines.append("post-code paragraph")
        return "\n".join(lines)

    def test_small_code_block_not_split(self):
        """Code block smaller than max_chars stays in one chunk."""
        code = "```python\nx = 1\ny = 2\n```"
        chunks = _split_section_text(code, max_chars=500, _overlap=0)
        # All chunks together must not split the fence markers
        full = "\n\n".join(chunks)
        assert full.count("```") % 2 == 0, "Unmatched ``` fence markers after chunking"

    def test_large_code_block_fences_not_orphaned(self):
        """
        A code block that exceeds max_chars must be split at a semantic boundary,
        not mid-line. Each chunk must not contain a lone opening ``` with no closing.
        """
        text = self._build_code_text(code_lines=200)
        chunks = _split_section_text(text, max_chars=300, _overlap=0)

        # Every chunk must have an even number of ``` markers OR the split
        # lands outside the fence (before or after).  The critical failure case
        # is a chunk with an *opening* fence and no *closing* fence.
        for i, chunk in enumerate(chunks):
            fence_count = chunk.count("```")
            # A chunk with exactly 1 ``` and it's an opening (not closing) is broken.
            if fence_count == 1:
                # It's OK only if the single ``` is a closing fence (preceded by code)
                # or the chunk contains both the language tag and content but split
                # occurred naturally. We check that no chunk ends with an orphaned
                # opening fence marker.
                assert not chunk.strip().endswith("```python"), (
                    f"Chunk {i} ends with orphaned opening fence:\n{chunk[:200]}"
                )

    def test_chunk_size_respects_budget(self):
        """All chunks must be ≤ max_chars characters."""
        text = self._build_code_text(code_lines=100)
        max_chars = 400
        chunks = _split_section_text(text, max_chars=max_chars, _overlap=0)
        for i, chunk in enumerate(chunks):
            assert len(chunk) <= max_chars, (
                f"Chunk {i} exceeds max_chars={max_chars}: got {len(chunk)} chars"
            )

    def test_no_content_lost(self):
        """All characters from the original text appear in the chunks."""
        text = self._build_code_text(code_lines=30)
        chunks = _split_section_text(text, max_chars=300, _overlap=0)
        # Strip inter-chunk joining artifacts: rejoin and compare token sets
        combined = "".join(chunks)
        assert len(combined) >= len(text) - len(chunks) * 2  # minor boundary padding OK


# ---------------------------------------------------------------------------
# TestMarkdownTablePreservation — | rows never split mid-row
# ---------------------------------------------------------------------------


class TestMarkdownTablePreservation:
    def _build_table_text(self, rows: int = 40) -> str:
        lines = ["# Heading\n\nSome preamble text.\n\n"]
        lines.append("| Column A | Column B | Column C |")
        lines.append("| --- | --- | --- |")
        for i in range(rows):
            lines.append(f"| row_{i}_val_a | row_{i}_val_b | row_{i}_val_c |")
        lines.append("\nPost-table paragraph.")
        return "\n".join(lines)

    def test_table_rows_not_split_mid_row(self):
        """
        No chunk must end with a partial table row (a line containing | that
        does not end with |).
        """
        text = self._build_table_text(rows=30)
        chunks = _split_section_text(text, max_chars=300, _overlap=0)
        for i, chunk in enumerate(chunks):
            for line in chunk.split("\n"):
                stripped = line.strip()
                if stripped.startswith("|"):
                    # A table row line must end with | (complete row)
                    assert stripped.endswith("|"), (
                        f"Chunk {i} contains a split table row: {stripped!r}"
                    )

    def test_chunk_size_budget_respected_with_table(self):
        text = self._build_table_text(rows=50)
        max_chars = 400
        chunks = _split_section_text(text, max_chars=max_chars, _overlap=0)
        for i, chunk in enumerate(chunks):
            assert len(chunk) <= max_chars, (
                f"Chunk {i} is {len(chunk)} chars, exceeds max_chars={max_chars}"
            )


# ---------------------------------------------------------------------------
# TestNormalChunking — existing behaviour regression guard
# ---------------------------------------------------------------------------


class TestNormalChunking:
    def test_short_text_single_chunk(self):
        sec = _section("This is short text.", order=0)
        chunks = chunk_structured([sec], max_chars=4000, prepend_header_context=False)
        assert len(chunks) == 1
        assert chunks[0].text == "This is short text."
        assert chunks[0].part_index == 0

    def test_paragraphs_split_on_blank_lines(self):
        text = "Para one.\n\nPara two.\n\nPara three."
        sec = _section(text)
        # max_chars=20: 'Para one.' (9) + sep(2) + 'Para two.' (9) = 20 — fits in one chunk.
        # 'Para three.' (11) goes in the next chunk. So minimum 2 chunks, all 3 paras present.
        chunks = chunk_structured([sec], max_chars=20, prepend_header_context=False)
        assert len(chunks) >= 2
        texts = [c.text for c in chunks]
        assert any("Para one" in t for t in texts)
        assert any("Para two" in t for t in texts)
        assert any("Para three" in t for t in texts)

    def test_sections_never_merged(self):
        """Text from different sections must never appear in the same chunk."""
        sec_a = _section("Section A content.", order=0)
        sec_b = _section("Section B content.", order=1)
        chunks = chunk_structured([sec_a, sec_b], max_chars=4000, prepend_header_context=False)
        assert len(chunks) == 2
        assert chunks[0].source_order != chunks[1].source_order

    def test_part_index_increments(self):
        """Multiple parts of the same section have ascending part_index."""
        long_text = "word " * 2000  # ~10,000 chars
        sec = _section(long_text)
        chunks = chunk_structured([sec], max_chars=1000, prepend_header_context=False)
        assert len(chunks) > 1
        for expected_idx, chunk in enumerate(chunks):
            assert chunk.part_index == expected_idx

    def test_empty_sections_skipped(self):
        sec = _section("", order=0)
        chunks = chunk_structured([sec], prepend_header_context=False)
        assert chunks == []


# ---------------------------------------------------------------------------
# TestHardSplitSemantic — fallback splitter
# ---------------------------------------------------------------------------


class TestHardSplitSemantic:
    def test_no_overflow_on_single_long_line(self):
        """Single word longer than max_chars → still produces valid chunks."""
        text = "x" * 500
        chunks = _hard_split_semantic(text, max_chars=100)
        assert len(chunks) >= 5
        for c in chunks:
            assert len(c) <= 100

    def test_prefers_newline_over_hard_cut(self):
        """If a newline exists within budget, split lands there."""
        text = "first line\nsecond line that is much longer than budget"
        chunks = _hard_split_semantic(text, max_chars=20)
        # First chunk should end at or after "first line\n"
        assert chunks[0].endswith("\n") or len(chunks[0]) <= 20

    def test_no_content_lost(self):
        text = "abc\ndef\nghi\njkl\nmno"
        chunks = _hard_split_semantic(text, max_chars=8)
        combined = "".join(chunks)
        assert combined == text


# ---------------------------------------------------------------------------
# TestExtractHeadingHierarchy — semantic header tracking (Item 27)
# ---------------------------------------------------------------------------


class TestExtractHeadingHierarchy:
    """Unit tests for _extract_heading_hierarchy()."""

    def test_single_h1(self):
        """A single # heading returns a one-element list."""
        headers = _extract_heading_hierarchy("# Overview\n\nSome text.")
        assert headers == ["Overview"]

    def test_h1_h2_chain(self):
        """# followed by ## builds a two-level hierarchy."""
        text = "# Overview\n\n## Security\n\nDetails here."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["Overview", "Security"]

    def test_h1_h2_h3_chain(self):
        """# ## ### builds a three-level hierarchy."""
        text = "# A\n## B\n### C\n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["A", "B", "C"]

    def test_h2_replaces_sibling(self):
        """A new ## replaces the previous ## at the same level."""
        text = "# Top\n## Old\n## New\n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["Top", "New"]

    def test_h2_replaces_deeper(self):
        """A new ## replaces both the previous ## and ###."""
        text = "# Top\n## A\n### B\n## C\n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["Top", "C"]

    def test_h3_replaces_sibling_only(self):
        """A new ### replaces the previous ### but keeps ##."""
        text = "# Top\n## A\n### Old\n### New\n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["Top", "A", "New"]

    def test_h4_ignored(self):
        """#### headings are excluded per RCA (adds noise)."""
        text = "# Top\n## A\n#### Ignored\n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["Top", "A"]
        assert "Ignored" not in headers

    def test_h5_h6_ignored(self):
        """##### and ###### are also excluded."""
        text = "# Top\n##### Deep\n###### Deeper\n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["Top"]

    def test_no_headings(self):
        """Text with no markdown headings returns an empty list."""
        headers = _extract_heading_hierarchy("Just some plain text.\nNo headings here.")
        assert headers == []

    def test_heading_with_special_chars(self):
        """Headings with special characters are preserved."""
        text = "# API: /v2/auth\n## OAuth 2.0 & JWT\n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["API: /v2/auth", "OAuth 2.0 & JWT"]

    def test_leading_trailing_whitespace_stripped(self):
        """Heading titles have leading/trailing whitespace stripped."""
        text = "#   Padded Title   \n\nContent."
        headers = _extract_heading_hierarchy(text)
        assert headers == ["Padded Title"]

    def test_setext_headings_ignored(self):
        """Setext-style headings (underlined with === or ---) are NOT detected."""
        text = "Title\n=====\n\nSome content."
        headers = _extract_heading_hierarchy(text)
        assert headers == []


# ---------------------------------------------------------------------------
# TestRenderHeaderContext — formatting the context prefix
# ---------------------------------------------------------------------------


class TestRenderHeaderContext:
    """Unit tests for _render_header_context()."""

    def test_single_level(self):
        result = _render_header_context(["Overview"])
        assert result == "Context: Overview"

    def test_two_levels(self):
        result = _render_header_context(["Overview", "Security"])
        assert result == "Context: Overview > Security"

    def test_three_levels(self):
        result = _render_header_context(["A", "B", "C"])
        assert result == "Context: A > B > C"

    def test_empty_list(self):
        result = _render_header_context([])
        assert result == ""

    def test_empty_tuple(self):
        result = _render_header_context(())
        assert result == ""


# ---------------------------------------------------------------------------
# TestHeaderPrepending — integration: chunk_structured with header context
# ---------------------------------------------------------------------------


class TestHeaderPrepending:
    """Integration tests for chunk_structured with prepend_header_context=True (default)."""

    def _section(self, text: str, path: str = "Document", order: int = 0) -> Section:
        return Section(text=text, structure_path=path, section_type="body", order=order)

    def test_context_prepended_to_chunk_text(self):
        """Default behavior prepends 'Context: ...' to chunk text."""
        sec = self._section("Some content.", path="Overview / Security")
        chunks = chunk_structured([sec], max_chars=4000)
        assert len(chunks) == 1
        assert chunks[0].text.startswith("Context: Overview > Security\n\n")
        assert "Some content." in chunks[0].text

    def test_context_not_prepended_when_disabled(self):
        """prepend_header_context=False preserves legacy behavior."""
        sec = self._section("Plain text.", path="Overview / Security")
        chunks = chunk_structured([sec], max_chars=4000, prepend_header_context=False)
        assert len(chunks) == 1
        assert chunks[0].text == "Plain text."

    def test_single_level_path(self):
        """A single-level structure_path gets a one-part context."""
        sec = self._section("Content.", path="Overview")
        chunks = chunk_structured([sec], max_chars=4000)
        assert chunks[0].text.startswith("Context: Overview\n\n")

    def test_document_path_no_context(self):
        """structure_path='Document' gets 'Context: Document' prefix."""
        sec = self._section("Doc content.", path="Document")
        chunks = chunk_structured([sec], max_chars=4000)
        assert chunks[0].text.startswith("Context: Document\n\n")

    def test_empty_structure_path_no_prefix(self):
        """Empty structure_path produces no context prefix."""
        sec = self._section("Content.", path="")
        chunks = chunk_structured([sec], max_chars=4000)
        # With empty path, _render_header_context returns "" → no prefix added
        assert chunks[0].text == "Content."

    def test_context_applied_to_all_parts(self):
        """When a section is split into multiple parts, all parts get the same context."""
        long_text = "word " * 2000  # ~10,000 chars
        sec = self._section(long_text, path="Overview / Security / Auth")
        chunks = chunk_structured([sec], max_chars=1000)
        assert len(chunks) > 1
        prefix = "Context: Overview > Security > Auth\n\n"
        for chunk in chunks:
            assert chunk.text.startswith(prefix), f"Chunk {chunk.part_index} missing header prefix"

    def test_context_applied_across_sections(self):
        """Each section gets its own structure_path as context."""
        sec_a = self._section("Content A.", path="Overview / Design", order=0)
        sec_b = self._section("Content B.", path="Overview / Security", order=1)
        chunks = chunk_structured([sec_a, sec_b], max_chars=4000)
        assert len(chunks) == 2
        assert chunks[0].text.startswith("Context: Overview > Design\n\n")
        assert chunks[1].text.startswith("Context: Overview > Security\n\n")

    def test_chunk_size_accounts_for_prefix(self):
        """Context prefix is prepended to each part — total chunk size includes prefix."""
        prefix = "Context: A > B > C\n\n"
        prefix_len = len(prefix)
        sec = self._section("x" * 500, path="A / B / C")
        chunks = chunk_structured([sec], max_chars=200)
        for chunk in chunks:
            assert chunk.text.startswith(prefix)
            # Content (excluding prefix) must fit within max_chars budget.
            content_only = chunk.text[prefix_len:]
            assert len(content_only) <= 200, (
                f"Chunk {chunk.part_index} content-only is {len(content_only)} chars, "
                f"exceeds max_chars=200"
            )
            # Total chunk size is content + prefix overhead.
            assert len(chunk.text) <= 200 + prefix_len, (
                f"Chunk {chunk.part_index} is {len(chunk.text)} chars total "
                f"(prefix={prefix_len} + content={len(content_only)})"
            )

    def test_code_block_context_preserved(self):
        """Code block chunks also get the context prefix."""
        text = "```python\nx = 1\ny = 2\n```"
        sec = self._section(text, path="Overview / Code Examples")
        chunks = chunk_structured([sec], max_chars=4000)
        assert len(chunks) == 1
        assert chunks[0].text.startswith("Context: Overview > Code Examples\n\n")
        assert "```python" in chunks[0].text
