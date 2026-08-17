"""J.11 PDF: pypdf → pdfminer → Tesseract; J.19 OCR path; optional pdfplumber tables."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from dataclasses import replace
from importlib import util as importlib_util

from nce.extractors.common import (
    cell_to_str,
    is_pdf_encrypted_blob,
    looks_garbled,
    rows_to_markdown,
    trim_trailing_empty,
)
from nce.extractors.core import ExtractionResult, Section, empty_skipped
from nce.extractors.ocr import ocr_pdf_to_sections

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decompression bomb guard
# ---------------------------------------------------------------------------

MAX_DECOMPRESSED_SIZE = 500 * 1024 * 1024  # 500 MB


def _check_pdf_bomb(blob: bytes) -> str | None:
    """Return an error message if *blob* looks like a decompression bomb.

    Two-tier check:
    1. Raw blob size itself exceeds the limit → reject immediately.
    2. Use ``pypdf`` to sum compressed stream lengths — if any single
       stream declares a huge uncompressed size, reject.
    """
    if len(blob) > MAX_DECOMPRESSED_SIZE:
        return (
            f"decompression_bomb: raw blob {len(blob)} bytes "
            f"exceeds MAX_DECOMPRESSED_SIZE ({MAX_DECOMPRESSED_SIZE})"
        )

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(blob))
        # Sum declared stream lengths as a conservative estimate.
        total_stream = 0
        for page in reader.pages:
            for key in page.get_object().keys():  # type: ignore[union-attr]
                obj = page.get_object()[key]  # type: ignore[index]
                if hasattr(obj, "get"):
                    length = obj.get("/Length", 0)
                    if isinstance(length, int):
                        total_stream += length
        if total_stream > MAX_DECOMPRESSED_SIZE:
            return (
                f"decompression_bomb: estimated {total_stream} decompressed bytes "
                f"exceeds MAX_DECOMPRESSED_SIZE ({MAX_DECOMPRESSED_SIZE})"
            )
    except Exception:
        # If pypdf can't even open the PDF, surface the error during extraction.
        pass

    return None


MIN_TEXT_FOR_SKIP_OCR = 200


def _pymupdf_extract_sync(blob: bytes) -> tuple[str, list[Section], list[str]]:
    warnings: list[str] = []
    sections: list[Section] = []
    texts: list[str] = []
    doc = None
    try:
        import gc

        import fitz  # PyMuPDF

        # Use 'with' context manager for PyMuPDF Document
        with fitz.open(stream=blob, filetype="pdf") as doc:
            for i, page in enumerate(doc, start=1):
                try:
                    t = page.get_text() or ""
                except Exception as e:
                    warnings.append(f"pymupdf_page_{i}:{e}")
                    t = ""
                t = t.strip()
                sections.append(
                    Section(
                        text=t,
                        structure_path=f"Page {i}",
                        section_type="body",
                        order=i - 1,
                    )
                )
                texts.append(t)
    except Exception as e:
        warnings.append(f"pymupdf_failed:{e}")
    finally:
        # Secondary defense layer: explicitly close the document to ensure native memory is freed
        if doc is not None:
            try:
                if not doc.is_closed:
                    doc.close()
            except Exception as e:
                warnings.append(f"pymupdf_close_failed:{e}")
        # Force garbage collection to reclaim native RAM immediately
        import gc

        gc.collect()

    return "\n\n".join(texts), sections, warnings


def _pypdf_extract_sync(blob: bytes) -> tuple[str, list[Section], list[str]]:
    from pypdf import PdfReader

    warnings: list[str] = []
    reader = PdfReader(io.BytesIO(blob))
    sections: list[Section] = []
    texts: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        t = ""
        try:
            t = page.extract_text() or ""
        except Exception as e:
            warnings.append(f"pypdf_page_{i}:{e}")
        t = t.strip()
        sections.append(
            Section(text=t, structure_path=f"Page {i}", section_type="body", order=i - 1)
        )
        texts.append(t)
    return "\n\n".join(texts), sections, warnings


def _pdfminer_extract_sync(blob: bytes) -> tuple[str, list[Section], list[str]]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    warnings: list[str] = []
    sections: list[Section] = []
    texts: list[str] = []
    try:
        pages = list(extract_pages(io.BytesIO(blob)))
    except Exception as e:
        log.warning("pdfminer extract_pages failed: %s", e)
        return "", [], [f"pdfminer_failed:{e}"]
    for i, page_layout in enumerate(pages, start=1):
        parts: list[str] = []
        try:
            for obj in page_layout:
                if isinstance(obj, LTTextContainer):
                    parts.append(obj.get_text())
        except Exception as e:
            warnings.append(f"pdfminer_page_{i}:{e}")
        t = "".join(parts).strip()
        sections.append(
            Section(text=t, structure_path=f"Page {i}", section_type="body", order=i - 1)
        )
        texts.append(t)
    return "\n\n".join(texts), sections, warnings


def _merge_pdfplumber_tables(blob: bytes, sections: list[Section], warnings: list[str]) -> None:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(blob)) as pdf:
            for i, page in enumerate(pdf.pages):
                page_num = i + 1
                try:
                    tables = page.extract_tables()
                except Exception as e:
                    warnings.append(f"pdfplumber_tables_page_{page_num}:{e}")
                    continue
                if not tables:
                    continue
                md_chunks: list[str] = []
                for tab in tables:
                    if not tab:
                        continue
                    rows = [tuple(cell_to_str(c) for c in (row or [])) for row in tab]
                    rows = trim_trailing_empty(rows)
                    if rows:
                        md_chunks.append(rows_to_markdown(rows))
                if not md_chunks:
                    continue
                extra = "\n\n## Tables (pdfplumber)\n\n" + "\n\n".join(md_chunks)
                if i < len(sections):
                    s = sections[i]
                    sections[i] = replace(s, text=(s.text + extra).strip())
                else:
                    warnings.append(f"pdfplumber_no_section_for_page:{page_num}")
    except Exception as e:
        warnings.append(f"pdfplumber_open_failed:{e}")


async def extract_pdf(blob: bytes) -> ExtractionResult:
    try:
        # Decompression bomb guard before any extraction.
        bomb_err = _check_pdf_bomb(blob)
        if bomb_err:
            return empty_skipped("pypdf", bomb_err)

        if is_pdf_encrypted_blob(blob):
            return empty_skipped("pypdf", "encrypted", warnings=["PDF /Encrypt detected"])

        warnings: list[str] = []

        # Check if fitz (PyMuPDF) is available without importing the heavy module unless present.
        try:
            has_fitz = importlib_util.find_spec("fitz") is not None
        except Exception:
            has_fitz = "fitz" in sys.modules

        if has_fitz:
            method = "pymupdf"
            text, sections, w = await asyncio.to_thread(_pymupdf_extract_sync, blob)
        else:
            method = "pypdf"
            text, sections, w = await asyncio.to_thread(_pypdf_extract_sync, blob)

        warnings.extend(w)

        if len(text.strip()) < MIN_TEXT_FOR_SKIP_OCR or looks_garbled(text):
            t2, s2, w2 = await asyncio.to_thread(_pdfminer_extract_sync, blob)
            warnings.extend(w2)
            if len(t2.strip()) > len(text.strip()) * 1.5:
                text, sections = t2, s2
                warnings.append("used_pdfminer_fallback")
                method = f"{method}+pdfminer"

        if len(text.strip()) < MIN_TEXT_FOR_SKIP_OCR:
            text, sections, w3 = await ocr_pdf_to_sections(blob)
            warnings.extend(w3)
            method = (
                f"{method}+pdfminer+tesseract" if "+pdfminer" in method else f"{method}+tesseract"
            )

        try:
            await asyncio.to_thread(_merge_pdfplumber_tables, blob, sections, warnings)
        except Exception as e:
            warnings.append(f"pdfplumber_merge_failed:{e}")
            log.debug("pdfplumber merge: %s", e)

        full_text = "\n\n".join(s.text for s in sections)
        metadata = {"page_sections": len(sections), "method_chain": method}
        import gc

        gc.collect()
        return ExtractionResult(
            method=method,
            text=full_text,
            sections=sections,
            metadata=metadata,
            warnings=warnings,
        )
    except Exception as exc:
        log.warning("extract_pdf failed on corrupted blob: %s", exc)
        return empty_skipped("pypdf", "corrupt", warnings=[f"extract_pdf_failed:{exc}"])
