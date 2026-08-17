import asyncio
import io
import zipfile
from unittest.mock import patch

import pytest

from nce.extractors.chunking import chunk_structured
from nce.extractors.core import Section
from nce.extractors.dispatch import extract_with_fallback
from nce.extractors.office_word import _check_zip_bomb
from nce.extractors.pdf_ext import _check_pdf_bomb


@patch("nce.extractors.dispatch.ensure_registered")
@patch("nce.extractors.dispatch._REGISTRY", new_callable=dict)
def test_extract_with_fallback_unsupported_extension(mock_registry, mock_ensure):
    # Test that unsupported extensions return a graceful skip instead of crashing
    result = asyncio.run(extract_with_fallback(b"dummy data", filename="test.unknownext"))
    assert result.skipped is True
    assert result.skip_reason == "unsupported_format"
    assert "unknown or unregistered extension" in result.warnings[0]


@patch("nce.extractors.dispatch.ensure_registered")
@patch("nce.extractors.dispatch._REGISTRY", new_callable=dict)
def test_extract_with_fallback_malformed_pdf(mock_registry, mock_ensure):
    # Mock the registry to have a failing PDF extractor
    async def mock_pdf_extractor(blob):
        raise ValueError("Malformed PDF bytes")

    mock_registry["pdf"] = mock_pdf_extractor

    # Test that a malformed PDF doesn't crash the system but returns a failure/skip
    result = asyncio.run(extract_with_fallback(b"not a real pdf", filename="test.pdf"))
    assert result.skipped is True
    assert result.skip_reason == "extraction_failed"
    assert len(result.warnings) > 0
    assert "Malformed PDF bytes" in result.warnings[0]


def test_chunk_structured_basic():
    sections = [Section(text="Short text.", structure_path="/p", section_type="paragraph", order=0)]
    chunks = chunk_structured(sections, max_chars=100, overlap=10, prepend_header_context=False)
    assert len(chunks) == 1
    assert chunks[0].text == "Short text."
    assert chunks[0].structure_path == "/p"
    assert chunks[0].source_order == 0


def test_chunk_structured_long_text_split():
    # Create a long text with paragraphs
    para1 = "A" * 60
    para2 = "B" * 60
    text = f"{para1}\n\n{para2}"

    sections = [Section(text=text, structure_path="/p", section_type="paragraph", order=0)]

    # max_chars=100 means para1 (60) + \n\n (2) + para2 (60) = 122 > 100
    # So it should split into two chunks
    chunks = chunk_structured(sections, max_chars=100, overlap=0, prepend_header_context=False)

    assert len(chunks) == 2
    assert chunks[0].text == para1
    assert chunks[1].text == para2
    assert chunks[0].part_index == 0
    assert chunks[1].part_index == 1


def test_chunk_structured_no_cross_section_merging():
    # Ensure that two short sections are NOT merged into one chunk
    sections = [
        Section(text="Section 1", structure_path="/s1", section_type="h1", order=0),
        Section(text="Section 2", structure_path="/s2", section_type="h1", order=1),
    ]

    chunks = chunk_structured(sections, max_chars=1000, overlap=0, prepend_header_context=False)
    assert len(chunks) == 2
    assert chunks[0].text == "Section 1"
    assert chunks[1].text == "Section 2"


# ---------------------------------------------------------------------------
# Decompression bomb tests
# ---------------------------------------------------------------------------


def _make_zip_with_sizes(entry_data: list[bytes]) -> bytes:
    """Create an in-memory ZIP file with the given entry data bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, data in enumerate(entry_data):
            zf.writestr(f"entry_{i}.bin", data)
    return buf.getvalue()


class TestCheckZipBomb:
    """Tests for ``_check_zip_bomb()`` — total and per-entry decompression limits."""

    def test_small_zip_passes(self, monkeypatch: pytest.MonkeyPatch):
        """A small zip below all thresholds should return None (pass)."""
        monkeypatch.setattr("nce.extractors.office_word.MAX_DECOMPRESSED_SIZE", 10_000)
        monkeypatch.setattr("nce.extractors.office_word.MAX_ENTRY_DECOMPRESSED_SIZE", 5_000)

        blob = _make_zip_with_sizes([b"A" * 100, b"B" * 200])
        assert _check_zip_bomb(blob) is None

    def test_total_exceeds_limit(self, monkeypatch: pytest.MonkeyPatch):
        """Total uncompressed size exceeding MAX_DECOMPRESSED_SIZE should be rejected."""
        monkeypatch.setattr("nce.extractors.office_word.MAX_DECOMPRESSED_SIZE", 250)
        monkeypatch.setattr("nce.extractors.office_word.MAX_ENTRY_DECOMPRESSED_SIZE", 500)

        blob = _make_zip_with_sizes([b"X" * 150, b"Y" * 150])
        err = _check_zip_bomb(blob)
        assert err is not None
        assert "decompression_bomb" in err

    def test_entry_exceeds_limit(self, monkeypatch: pytest.MonkeyPatch):
        """Single entry exceeding MAX_ENTRY_DECOMPRESSED_SIZE should be rejected."""
        monkeypatch.setattr("nce.extractors.office_word.MAX_DECOMPRESSED_SIZE", 10_000)
        monkeypatch.setattr("nce.extractors.office_word.MAX_ENTRY_DECOMPRESSED_SIZE", 100)

        blob = _make_zip_with_sizes([b"X" * 200, b"Y" * 50])
        err = _check_zip_bomb(blob)
        assert err is not None
        assert "decompression_bomb" in err
        assert "MAX_ENTRY_DECOMPRESSED_SIZE" in err

    def test_corrupt_zip_returns_error(self):
        """A corrupted blob should return an error, not crash."""
        err = _check_zip_bomb(b"not-a-zip-file")
        assert err is not None
        assert "zip_scan_failed" in err


class TestCheckPdfBomb:
    """Tests for ``_check_pdf_bomb()`` — decompression limit on PDF streams."""

    def test_small_pdf_passes(self, monkeypatch: pytest.MonkeyPatch):
        """A minimal PDF below thresholds should return None."""
        monkeypatch.setattr("nce.extractors.pdf_ext.MAX_DECOMPRESSED_SIZE", 10_000)
        # Minimal valid PDF
        blob = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
            b"xref\n0 3\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
            b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n107\n%%EOF"
        )
        assert _check_pdf_bomb(blob) is None

    def test_pdf_too_large(self, monkeypatch: pytest.MonkeyPatch):
        """A PDF blob exceeding MAX_DECOMPRESSED_SIZE should be rejected."""
        monkeypatch.setattr("nce.extractors.pdf_ext.MAX_DECOMPRESSED_SIZE", 100)
        blob = b"X" * 200  # Exceeds the 100-byte limit
        err = _check_pdf_bomb(blob)
        assert err is not None
        assert "decompression_bomb" in err


def test_empty_skipped_import():
    """Verify empty_skipped is importable and returns correct type."""
    from nce.extractors.core import empty_skipped as es

    result = es("test_extractor", "bomb_detected")
    assert result.skipped is True
    assert result.skip_reason == "bomb_detected"


def test_pymupdf_extract_hygiene(monkeypatch: pytest.MonkeyPatch):
    """Test that _pymupdf_extract_sync uses context manager, calls close, and triggers gc.collect()."""
    import sys
    from unittest.mock import MagicMock

    mock_page = MagicMock()
    mock_page.get_text.return_value = "Page text from PyMuPDF " * 15

    mock_doc = MagicMock()
    # Support 'with' context manager returning itself
    mock_doc.__enter__.return_value = mock_doc
    mock_doc.__exit__.return_value = False
    # Support iteration yielding pages
    mock_doc.__iter__.return_value = [mock_page]
    mock_doc.is_closed = False

    mock_fitz = MagicMock()
    mock_fitz.open.return_value = mock_doc

    # Patch sys.modules to simulate fitz being installed
    monkeypatch.setitem(sys.modules, "fitz", mock_fitz)

    # Also spy on gc.collect
    gc_collect_called = []
    import gc

    original_gc_collect = gc.collect

    def mock_gc_collect(*args, **kwargs):
        gc_collect_called.append(True)
        return original_gc_collect(*args, **kwargs)

    monkeypatch.setattr(gc, "collect", mock_gc_collect)

    # Now run the sync extractor
    from nce.extractors.pdf_ext import _pymupdf_extract_sync

    blob = b"dummy_pdf_bytes"
    text, sections, warnings = _pymupdf_extract_sync(blob)

    # Assert fitz.open was called with correct parameters
    mock_fitz.open.assert_called_once_with(stream=blob, filetype="pdf")
    # Assert text was correctly extracted
    assert "Page text from PyMuPDF" in text
    assert len(sections) == 1
    # Assert gc.collect was triggered
    assert len(gc_collect_called) > 0
    # Assert mock_doc.close was called in the finally block
    mock_doc.close.assert_called_once()


@pytest.mark.asyncio
async def test_extract_pdf_pymupdf_fallback_to_pypdf(monkeypatch: pytest.MonkeyPatch):
    """Verify that extract_pdf falls back to pypdf when fitz is not installed."""
    import sys
    from unittest.mock import MagicMock

    # Ensure 'fitz' is not in sys.modules
    monkeypatch.setitem(sys.modules, "fitz", None)

    # Mock pypdf so the test doesn't depend on real pypdf being installed
    mock_pypdf = MagicMock()
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Mocked pypdf extracted text " * 15
    mock_reader.pages = [mock_page]
    mock_pypdf.PdfReader.return_value = mock_reader
    monkeypatch.setitem(sys.modules, "pypdf", mock_pypdf)

    from nce.extractors.pdf_ext import extract_pdf

    blob = b"dummy_pdf_bytes"
    result = await extract_pdf(blob)
    assert "pypdf" in result.method
    assert "Mocked pypdf" in result.text


@pytest.mark.asyncio
async def test_extract_pdf_uses_pymupdf_when_available(monkeypatch: pytest.MonkeyPatch):
    """Verify extract_pdf uses pymupdf when fitz is available."""
    import sys
    from unittest.mock import MagicMock

    mock_page = MagicMock()
    mock_page.get_text.return_value = "Extracted via PyMuPDF! " * 15

    mock_doc = MagicMock()
    mock_doc.__enter__.return_value = mock_doc
    mock_doc.__exit__.return_value = False
    mock_doc.__iter__.return_value = [mock_page]
    mock_doc.is_closed = False

    mock_fitz = MagicMock()
    mock_fitz.open.return_value = mock_doc

    monkeypatch.setitem(sys.modules, "fitz", mock_fitz)

    from nce.extractors.pdf_ext import extract_pdf

    blob = b"%PDF-1.4 mock pdf"
    result = await extract_pdf(blob)

    assert "pymupdf" in result.method
    assert "Extracted via PyMuPDF!" in result.text
