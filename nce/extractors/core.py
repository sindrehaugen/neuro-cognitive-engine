"""
Common extraction schema (Appendix J.2).

chunk_structured() (see chunking.py) must treat each Section as an atomic boundary:
chunks are built only from text belonging to a single Section. Splitting a long Section
into multiple parts is allowed (same structure_path / order, distinct part_index); merging
text from two Sections (e.g. two Excel sheets or two slides) is never allowed, so semantic
chunking cannot splice across sheet or slide boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Section", "ExtractionResult", "empty_skipped"]


@dataclass
class Section:
    """One addressable region of the source document for indexing and citation."""

    text: str
    structure_path: str
    section_type: str  # heading, body, table, slide, sheet, note, comment, footer, metadata, ...
    order: int


@dataclass
class ExtractionResult:
    """Unified extractor output (Appendix J.2)."""

    method: str
    text: str
    sections: list[Section]
    metadata: dict[str, Any]
    warnings: list[str]
    skipped: bool = False
    skip_reason: str | None = None


def empty_skipped(
    method: str,
    reason: str,
    *,
    warnings: list[str] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        method=method,
        text="",
        sections=[],
        metadata={},
        warnings=list(warnings or []),
        skipped=True,
        skip_reason=reason,
    )
