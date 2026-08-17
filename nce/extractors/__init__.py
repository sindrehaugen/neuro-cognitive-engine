"""Document extractors (Appendix J): schema, chunking, format drivers, dispatch."""

from nce.extractors.chunking import StructuredChunk, chunk_structured
from nce.extractors.core import ExtractionResult, Section, empty_skipped

# J.14 API (OAuth); invoke from workers with board/document id + token.
from nce.extractors.diagram_api import (
    lucidchart_extract_document,
    miro_extract_board,
)
from nce.extractors.dispatch import (
    ensure_registered,
    extension_from_filename,
    extract_bytes,
    extract_with_fallback,
    register_extension,
    register_mime,
)
from nce.extractors.encryption import (
    detect_encryption,
    extraction_encrypted_skip,
    maybe_encrypted_skip,
)

__all__ = [
    "chunk_structured",
    "detect_encryption",
    "empty_skipped",
    "extraction_encrypted_skip",
    "lucidchart_extract_document",
    "maybe_encrypted_skip",
    "miro_extract_board",
    "ensure_registered",
    "extension_from_filename",
    "extract_bytes",
    "extract_with_fallback",
    "register_extension",
    "register_mime",
    "Section",
    "ExtractionResult",
    "StructuredChunk",
]
