"""Security regression tests for extractors batch E1 (production hardening)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.extractors import libreoffice
from nce.extractors.dispatch import _is_security_relevant_mismatch, extract_bytes
from nce.extractors.libreoffice import _safe_source_ext


class TestLibreOfficeExt:
    def test_safe_source_ext_rejects_path_segments(self):
        with pytest.raises(ValueError):
            _safe_source_ext("../../../evil.doc")

    def test_libreoffice_convert_invalid_ext_returns_none(self):
        assert libreoffice.libreoffice_convert(b"x", "../x", "docx") is None


class TestDispatchMimePolyglot:
    def test_pdf_extension_zip_magic_is_mismatch(self):
        assert _is_security_relevant_mismatch("application/pdf", "application/zip")

    @pytest.mark.asyncio
    async def test_zip_bytes_named_pdf_skipped(self):
        zip_head = b"PK\x03\x04" + b"\x00" * 64
        with (
            patch("nce.extractors.dispatch.ensure_registered"),
            patch("nce.extractors.dispatch._REGISTRY", {"pdf": AsyncMock()}),
        ):
            result = await extract_bytes(zip_head, filename="evil.pdf")
        assert result.skip_reason == "mime_mismatch"


class TestDiagramApiBoardId:
    @pytest.mark.asyncio
    async def test_invalid_board_id_skipped_without_http(self):
        from nce.extractors.diagram_api import miro_extract_board

        with patch("nce.extractors.diagram_api.validate_extractor_url"):
            result = await miro_extract_board(
                "foo/bar",
                access_token="tok",
                base_url="https://api.miro.com/v2",
            )
        assert result.skip_reason == "invalid_board_id"


class TestOcrLimits:
    @pytest.mark.asyncio
    async def test_ocr_pdf_page_limit_warning(self, monkeypatch):
        from nce.extractors import ocr as ocr_mod

        monkeypatch.setattr(ocr_mod, "_MAX_OCR_PAGES", 2)

        fake_pages = [MagicMock(), MagicMock()]
        fake_pdf2image = MagicMock()
        fake_pdf2image.convert_from_bytes = MagicMock(return_value=fake_pages)

        with patch.dict("sys.modules", {"pdf2image": fake_pdf2image}):
            with patch.object(ocr_mod.asyncio, "to_thread", new=AsyncMock(return_value=fake_pages)):
                with patch.object(ocr_mod, "ocr_pil_image", new=AsyncMock(return_value=("t", []))):
                    _text, _sections, warnings = await ocr_mod.ocr_pdf_to_sections(b"%PDF")

        assert any("ocr_page_limit" in w for w in warnings)
