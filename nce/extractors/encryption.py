"""
Appendix J.18 — encrypted / password-protected file detection.

RCA (error handling): Detection must never throw into the worker; all sniffers are
wrapped in try/except and return False on ambiguity. False negatives are acceptable
for edge formats; false positives yield a skipped ExtractionResult (never a crash).
Production workers should log ``metadata["audit_log"]`` to the deployment audit sink.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from nce.extractors.common import is_pdf_encrypted_blob, is_zip_encrypted_ooxml
from nce.extractors.core import ExtractionResult

log = logging.getLogger(__name__)


def extraction_encrypted_skip(
    *,
    filename: str | None,
    format_hint: str,
    detail: str | None = None,
) -> ExtractionResult:
    """Uniform skip payload for encrypted content (J.18 §1–3)."""
    audit: dict[str, Any] = {
        "event": "skipped_encrypted",
        "filename": filename,
        "format": format_hint,
    }
    if detail:
        audit["detail"] = detail
    msg = f"Encrypted document not indexed: {filename or '(unknown file)'} ({format_hint})"
    if detail:
        msg = f"{msg} — {detail}"
    return ExtractionResult(
        method="encryption_sniff",
        text="",
        sections=[],
        metadata={"audit_log": audit},
        warnings=[msg],
        skipped=True,
        skip_reason="encrypted",
    )


def is_zip_archive_encrypted(blob: bytes) -> bool:
    """ZIP member uses traditional PKWARE encryption (general purpose flag bit 0)."""
    if len(blob) < 4 or blob[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for info in z.infolist():
                if info.flag_bits & 0x1:
                    return True
    except zipfile.BadZipFile:
        return False
    except Exception as e:
        log.debug("zip_encrypt_sniff_error: %s", e)
        return False
    return False


def is_ole_encrypted_or_encryptedpackage(blob: bytes) -> bool:
    """Best-effort OLE compound encryption (legacy Office, some containers)."""
    try:
        import olefile
    except ImportError:
        return False
    try:
        if not olefile.isOleFile(io.BytesIO(blob)):
            return False
        ole = olefile.OleFileIO(io.BytesIO(blob))
        try:
            parts = ["/".join(x) for x in ole.listdir(streams=True) if isinstance(x, (list, tuple))]
            joined = " ".join(parts).lower()
            if "encryption" in joined or "encryptedpackage" in joined:
                return True
        finally:
            try:
                ole.close()
            except Exception:
                pass
    except Exception as e:
        log.debug("ole_encrypt_sniff_error: %s", e)
        return False
    try:
        import msoffcrypto  # type: ignore[import-untyped]

        f = msoffcrypto.OfficeFile(io.BytesIO(blob))
        if getattr(f, "is_encrypted", lambda: False)():
            return True
    except ImportError:
        pass
    except Exception as e:
        log.debug("msoffcrypto_sniff_error: %s", e)
    return False


def detect_encryption(
    blob: bytes,
    *,
    filename: str | None,
    extension: str | None,
) -> str | None:
    """
    Return a short reason string if the blob should be skipped as encrypted, else None.
    Order: PDF → OOXML markers → ZIP crypto flags → OLE / msoffcrypto.
    """
    ext = (extension or "").lower().lstrip(".")
    try:
        if is_pdf_encrypted_blob(blob):
            return "pdf_encrypted"
        if len(blob) >= 2 and blob[:2] == b"PK":
            if is_zip_encrypted_ooxml(blob):
                return "ooxml_encrypted"
            if ext == "zip" or ext.endswith("zip"):
                if is_zip_archive_encrypted(blob):
                    return "zip_member_encrypted"
            if ext in ("docx", "xlsx", "pptx", "idml"):
                if is_zip_encrypted_ooxml(blob):
                    return "ooxml_encrypted"
        if ext in ("doc", "xls", "ppt", "msg"):
            if is_ole_encrypted_or_encryptedpackage(blob):
                return "ole_encrypted"
        if ext in ("doc", "xls", "ppt") and blob[:2] != b"PK":
            if is_ole_encrypted_or_encryptedpackage(blob):
                return "ole_encrypted"
    except Exception as e:
        log.warning("detect_encryption internal error (treat as not detected): %s", e)
    return None


def maybe_encrypted_skip(
    blob: bytes,
    *,
    filename: str | None,
    extension: str | None,
) -> ExtractionResult | None:
    reason = detect_encryption(blob, filename=filename, extension=extension)
    if not reason:
        return None
    return extraction_encrypted_skip(filename=filename, format_hint=reason, detail=reason)
