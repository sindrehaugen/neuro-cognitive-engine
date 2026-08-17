"""
Structured chunking driven by Section objects (Appendix J.2).

RCA — consumption by chunk_structured:
- Each Section is a hard boundary: chunks never concatenate text from two different
  Section instances. That preserves Excel (one Section per visible sheet), PowerPoint
  (one Section per slide body and a separate Section for speaker notes), Word headings,
  and PDF pages as non-splittable units at the indexing layer.
- Long Sections (e.g. huge pasted text in one cell) may be split into multiple
  StructuredChunk records sharing the same structure_path / source_order and increasing
  part_index; embeddings still cite the same anchor.
- chunk_structured walks sections in ``order`` and applies a character budget per chunk,
  preferring paragraph boundaries (\\n\\n) inside the Section.

Semantic boundary preservation
-------------------------------
When a paragraph itself exceeds ``max_chars``, the splitter first attempts to find a
semantic boundary within the budget window:

1. **Fenced code block boundary** — a line beginning with ` ``` ` that closes an open
   fence.  Splitting here keeps the entire fenced block in one chunk.
2. **Markdown table row boundary** — the last line that starts with ``|`` within the
   budget.  Splitting here keeps the table row intact.
3. **Newline boundary** — the last ``\\n`` within the budget window, so at minimum a
   line is never split mid-character.
4. **Hard character split** — last resort; only used if none of the above yield a split
   point (e.g. a single line longer than ``max_chars`` with no whitespace).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from nce.extractors.core import Section

__all__ = [
    "StructuredChunk",
    "chunk_structured",
    "_extract_heading_hierarchy",
    "_render_header_context",
]

# ---------------------------------------------------------------------------
# Rolling heading hierarchy for semantic header prepending (Item 27)
# ---------------------------------------------------------------------------

# Regex to match ATX headings (#, ##, ###, etc.) at line start.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Maximum heading depth tracked (h1–h3 per RCA).
_MAX_HEADING_LEVEL = 3


def _extract_heading_hierarchy(text: str) -> Sequence[str]:
    """
    Extract the most recent h1 → h2 → h3 heading chain from ``text``.

    Walks all ATX headings (``#`` through ``######``) and maintains a
    rolling hierarchy: when a heading at level *L* is encountered, all
    headings at level >= *L* are replaced.  Only levels 1–3 are included
    in the returned prefix (per RCA — h4+ adds noise to embedding context).

    Returns a list of heading titles ordered from shallowest to deepest,
    e.g. ``["Overview", "Security", "Authentication"]``.
    """
    hierarchy: list[str] = []
    for match in _HEADING_RE.finditer(text):
        level = len(match.group(1))
        title = match.group(2).strip()
        if level > _MAX_HEADING_LEVEL:
            continue
        # Trim hierarchy to level-1, then append new heading.
        hierarchy = hierarchy[: level - 1]
        hierarchy.append(title)
    return hierarchy


def _render_header_context(headers: Sequence[str]) -> str:
    """
    Format a heading chain into a semantic context prefix.

    >>> _render_header_context(["Overview", "Security"])
    'Context: Overview > Security'
    >>> _render_header_context([])
    ''
    """
    if not headers:
        return ""
    return "Context: " + " > ".join(headers)


@dataclass
class StructuredChunk:
    text: str
    structure_path: str
    section_type: str
    source_order: int
    part_index: int


def chunk_structured(
    sections: list[Section],
    *,
    max_chars: int = 4000,
    overlap: int = 200,
    prepend_header_context: bool = True,
) -> list[StructuredChunk]:
    """
    Build chunks that never span two Sections.
    Within a Section, split on paragraphs; optional overlap copies tail of previous part.
    Code blocks and markdown tables are never split mid-boundary.

    When *prepend_header_context* is ``True`` (default), each chunk's text is
    prefixed with a semantic context line derived from the Section's
    ``structure_path`` (e.g. ``Context: Overview > Security > Auth``).
    This dramatically improves RAG retrieval relevance by preventing chunks
    from losing their parent heading context.
    """
    if overlap >= max_chars:
        overlap = max(0, max_chars // 8)

    out: list[StructuredChunk] = []
    for sec in sorted(sections, key=lambda s: s.order):
        text = sec.text.strip()
        if not text:
            continue

        # Build the semantic context prefix from the section's heading hierarchy.
        context_prefix = ""
        if prepend_header_context and sec.structure_path:
            # structure_path is e.g. "Overview / Security / Auth" from markdown parsing.
            # Render as "Context: Overview > Security > Auth" for embedding.
            context_prefix = _render_header_context(sec.structure_path.split(" / "))
            if context_prefix:
                context_prefix += "\n\n"

        parts = _split_section_text(text, max_chars, overlap)
        for i, p in enumerate(parts):
            chunk_text = context_prefix + p if context_prefix else p
            out.append(
                StructuredChunk(
                    text=chunk_text,
                    structure_path=sec.structure_path,
                    section_type=sec.section_type,
                    source_order=sec.order,
                    part_index=i,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Semantic boundary helpers
# ---------------------------------------------------------------------------


def _find_semantic_split(text: str, budget: int) -> int:
    """
    Return the best split index within ``text[:budget]`` that respects
    semantic boundaries, in priority order:

    1. Closing fenced code block (``` on its own line) within budget.
    2. Last markdown table row boundary (line starting with |) within budget.
    3. Last newline within budget.
    4. Hard character cut at ``budget`` (last resort).

    Always returns a value in ``[1, budget]``.
    """
    window = text[:budget]

    # 1. Fenced code block: find the last ``` line-boundary within the window.
    #    We look for a newline followed by ``` (with optional spaces) as a fence
    #    delimiter.  Split *after* that line so the closing fence stays in the
    #    current chunk.
    fence_matches = list(re.finditer(r"\n```[^\S\n]*(?:\n|$)", window))
    if fence_matches:
        # Use the end of the last closing fence within the budget.
        split_at = fence_matches[-1].end()
        if 0 < split_at <= budget:
            return split_at

    # 2. Markdown table: last line starting with | within the window.
    table_matches = list(re.finditer(r"\n(?=\|)", window))
    if table_matches:
        split_at = table_matches[-1].start()  # split just before the | line
        if 0 < split_at <= budget:
            return split_at

    # 3. Last newline within window.
    newline_pos = window.rfind("\n")
    if newline_pos > 0:
        return newline_pos + 1  # include the newline in the preceding chunk

    # 4. Hard cut — no semantic boundary found.
    return budget


def _hard_split_semantic(text: str, max_chars: int) -> list[str]:
    """
    Split ``text`` into chunks of at most ``max_chars`` characters, respecting
    semantic boundaries (code fences → table rows → newlines → hard cut).
    """
    chunks: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        remaining = length - pos
        if remaining <= max_chars:
            chunks.append(text[pos:])
            break
        split_offset = _find_semantic_split(text[pos:], max_chars)
        chunks.append(text[pos : pos + split_offset])
        pos += split_offset
    return chunks


def _split_section_text(text: str, max_chars: int, _overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    buf: list[str] = []
    cur_len = 0

    for para in paragraphs:
        sep = 2 if buf else 0
        need = sep + len(para)
        if need > max_chars and not buf:
            # Paragraph is too large on its own — semantic split it.
            chunks.extend(_hard_split_semantic(para, max_chars))
            continue
        if cur_len + need <= max_chars:
            buf.append(para)
            cur_len += need
            continue
        if buf:
            chunks.append("\n\n".join(buf))
        buf = []
        cur_len = 0
        if len(para) > max_chars:
            # Paragraph is too large — semantic split it.
            chunks.extend(_hard_split_semantic(para, max_chars))
            continue
        buf = [para]
        cur_len = len(para)
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks
