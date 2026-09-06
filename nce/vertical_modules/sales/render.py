"""
nce/vertical_modules/sales/render.py
====================================
Deterministic quote document PDF renderer for Sales quote signing.

Under Sindre's ruling (Option b), NCE renders the quote document directly from
the frozen baseline. The bytes signed are the bytes stored and the bytes sent.

Guarantees:
  1. Pure determinism: Same baseline and quote inputs produce byte-identical PDF bytes
     (identical SHA-256 hash across runs, environments, and processes).
  2. Zero wall-clock dependency: All timestamps displayed in the document are derived
     strictly from the baseline's `signed_at` record.
  3. Zero external network dependency: Generates valid ISO 32000-1 / PDF 1.4 bytes
     using built-in Type 1 standard fonts (Helvetica, Helvetica-Bold) without external
     font files, network calls, or third-party service dependencies.
  4. Cryptographic integrity: Document trailer carries a deterministic document ID
     derived from the baseline's natural key and frozen timestamp.
"""

from __future__ import annotations

import datetime
import hashlib
import io
from decimal import Decimal
from typing import Any


def _escape_pdf_text(text: str) -> str:
    """Escape special characters for PDF literal strings (parentheses and backslashes).

    Transliterates non-ASCII/non-latin-1 characters safely so text never corrupts
    Type 1 font encoding streams.
    """
    cleaned = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    # Ensure characters fit within latin-1 range safely for standard Type 1 fonts
    return cleaned.encode("latin-1", errors="replace").decode("latin-1")


def _format_currency(amount: float | Decimal | int, currency: str = "NOK") -> str:
    """Format an amount deterministically as standard currency string."""
    val = float(amount)
    return f"{val:,.2f} {currency}"


def render_quote_document(
    *,
    baseline: dict[str, Any],
    quote_details: dict[str, Any] | None = None,
    line_items: list[dict[str, Any]] | None = None,
    signer: dict[str, Any] | None = None,
) -> bytes:
    """Render a deterministic, immutable quote PDF from a frozen baseline.

    Args:
        baseline: Required baseline dict containing at minimum:
            - `quote_id` (str)
            - `signed_total_nok` (float | Decimal)
            - `signed_margin_pct` (float | Decimal)
            - `signed_at` (str | datetime.datetime)
        quote_details: Optional dict of quote attributes from `sales_read_model`
            (e.g., `name`, `customer_name`, `description`, `currency`).
        line_items: Optional list of line item dicts from `bom_line_content` or `lines`.
        signer: Optional signer identity dict with `name` and `email`.

    Returns:
        bytes: Raw, byte-identical PDF bytes.
    """
    if not baseline or not isinstance(baseline, dict):
        raise ValueError("baseline dict is required to render quote document")

    quote_id = str(baseline.get("quote_id", "")).strip()
    if not quote_id:
        raise ValueError("baseline must contain a non-empty quote_id")

    signed_total_nok = baseline.get("signed_total_nok")
    if signed_total_nok is None:
        raise ValueError("baseline must contain signed_total_nok")
    total_val = float(signed_total_nok)

    signed_margin_pct = baseline.get("signed_margin_pct")
    if signed_margin_pct is None:
        raise ValueError("baseline must contain signed_margin_pct")
    margin_val = float(signed_margin_pct)

    signed_at_raw = baseline.get("signed_at") or baseline.get("frozen_at_utc")
    if not signed_at_raw:
        raise ValueError("baseline must contain signed_at timestamp")

    if isinstance(signed_at_raw, datetime.datetime):
        frozen_iso = signed_at_raw.isoformat()
    else:
        frozen_iso = str(signed_at_raw)

    details = quote_details or {}
    quote_name = str(details.get("name") or f"Sales Proposal {quote_id}")
    customer_name = str(details.get("customer_name") or details.get("customer") or "Valued Client")
    currency = str(details.get("currency") or "NOK").upper()

    signer_dict = signer or {}
    signer_name = str(signer_dict.get("name") or "Authorized Representative")
    signer_email = str(signer_dict.get("email") or "pending-assignment@domain.invalid")

    # Sort line items deterministically if provided
    sorted_lines: list[dict[str, Any]] = []
    if line_items:
        sorted_lines = sorted(
            line_items,
            key=lambda item: (
                str(item.get("line_ref") or ""),
                str(item.get("item_name") or item.get("label") or ""),
            ),
        )

    # Build PDF Drawing Commands (Content Stream)
    # Coordinate system: origin (0, 0) at bottom-left of A4 page (595.28 x 841.89 pt)
    stream_ops: list[str] = []

    # 1. Header Banner & Title
    stream_ops.append("q")
    stream_ops.append("0.12 0.20 0.35 rg")  # Deep slate navy header bar
    stream_ops.append("50 780 495 2 re f")  # Top decorative horizontal bar
    stream_ops.append("Q")

    stream_ops.append("BT")
    stream_ops.append("/F1 20 Tf")  # Helvetica-Bold 20pt
    stream_ops.append("50 750 Td")
    stream_ops.append(f"({_escape_pdf_text(quote_name)}) Tj")
    stream_ops.append("ET")

    stream_ops.append("BT")
    stream_ops.append("/F2 11 Tf")  # Helvetica 11pt
    stream_ops.append("0.35 0.35 0.35 rg")
    stream_ops.append("50 732 Td")
    stream_ops.append(
        f"(Commercial Quotation & Frozen Agreement Baseline | Quote Ref: {_escape_pdf_text(quote_id)}) Tj"
    )
    stream_ops.append("ET")

    # 2. Metadata Section (Two-column layout)
    # Left column: Customer & Document Info
    stream_ops.append("BT")
    stream_ops.append("/F1 10 Tf")
    stream_ops.append("0 0 0 rg")
    stream_ops.append("50 695 Td")
    stream_ops.append("(Customer / Client:) Tj")
    stream_ops.append("/F2 10 Tf")
    stream_ops.append("110 0 Td")
    stream_ops.append(f"({_escape_pdf_text(customer_name)}) Tj")
    stream_ops.append("ET")

    stream_ops.append("BT")
    stream_ops.append("/F1 10 Tf")
    stream_ops.append("50 680 Td")
    stream_ops.append("(Baseline Frozen:) Tj")
    stream_ops.append("/F2 10 Tf")
    stream_ops.append("110 0 Td")
    stream_ops.append(f"({_escape_pdf_text(frozen_iso)}) Tj")
    stream_ops.append("ET")

    # Right column: Signer Info
    stream_ops.append("BT")
    stream_ops.append("/F1 10 Tf")
    stream_ops.append("320 695 Td")
    stream_ops.append("(Designated Signer:) Tj")
    stream_ops.append("/F2 10 Tf")
    stream_ops.append("105 0 Td")
    stream_ops.append(f"({_escape_pdf_text(signer_name)}) Tj")
    stream_ops.append("ET")

    stream_ops.append("BT")
    stream_ops.append("/F1 10 Tf")
    stream_ops.append("320 680 Td")
    stream_ops.append("(Signer Email:) Tj")
    stream_ops.append("/F2 10 Tf")
    stream_ops.append("105 0 Td")
    stream_ops.append(f"({_escape_pdf_text(signer_email)}) Tj")
    stream_ops.append("ET")

    # 3. Commercial Summary Box
    stream_ops.append("q")
    stream_ops.append("0.96 0.97 0.98 rg")  # Light gray background
    stream_ops.append("50 610 495 50 re f")
    stream_ops.append("0.80 0.82 0.85 RG 1 w")
    stream_ops.append("50 610 495 50 re S")
    stream_ops.append("Q")

    stream_ops.append("BT")
    stream_ops.append("/F1 11 Tf")
    stream_ops.append("0.15 0.25 0.40 rg")
    stream_ops.append("65 638 Td")
    stream_ops.append("(FROZEN COMMERCIAL TOTAL:) Tj")
    stream_ops.append("/F1 13 Tf")
    stream_ops.append("175 0 Td")
    stream_ops.append(f"({_format_currency(total_val, currency)}) Tj")
    stream_ops.append("ET")

    stream_ops.append("BT")
    stream_ops.append("/F1 10 Tf")
    stream_ops.append("0.3 0.3 0.3 rg")
    stream_ops.append("65 622 Td")
    stream_ops.append("(Frozen Gross Margin:) Tj")
    stream_ops.append("/F2 10 Tf")
    stream_ops.append("120 0 Td")
    stream_ops.append(f"({margin_val * 100:.1f}%) Tj")
    stream_ops.append("ET")

    # 4. Line Items Table (Header)
    y_pos = 575
    stream_ops.append("q")
    stream_ops.append("0.20 0.25 0.35 rg")
    stream_ops.append(f"50 {y_pos - 5} 495 18 re f")
    stream_ops.append("Q")

    stream_ops.append("BT")
    stream_ops.append("/F1 9 Tf")
    stream_ops.append("1 1 1 rg")
    stream_ops.append(f"55 {y_pos} Td")
    stream_ops.append("(Line / Ref) Tj")
    stream_ops.append("80 0 Td")
    stream_ops.append("(Description / Item) Tj")
    stream_ops.append("230 0 Td")
    stream_ops.append("(Qty) Tj")
    stream_ops.append("50 0 Td")
    stream_ops.append("(Unit Price) Tj")
    stream_ops.append("75 0 Td")
    stream_ops.append("(Total) Tj")
    stream_ops.append("ET")

    y_pos -= 22

    # Render line items if available, or summary row
    if sorted_lines:
        for idx, line in enumerate(sorted_lines[:15]):  # Keep single-page bound
            ref = str(line.get("line_ref") or f"L{idx + 1:02d}")[:12]
            item_desc = str(
                line.get("item_name")
                or line.get("description")
                or line.get("label")
                or "Equipment item"
            )[:38]
            qty_val = float(line.get("qty") or line.get("quantity") or 1.0)
            unit_p = float(line.get("unit_price") or 0.0)
            line_tot = float(line.get("line_total") or (qty_val * unit_p))

            stream_ops.append("BT")
            stream_ops.append("/F2 9 Tf")
            stream_ops.append("0.1 0.1 0.1 rg")
            stream_ops.append(f"55 {y_pos} Td")
            stream_ops.append(f"({_escape_pdf_text(ref)}) Tj")
            stream_ops.append("80 0 Td")
            stream_ops.append(f"({_escape_pdf_text(item_desc)}) Tj")
            stream_ops.append("230 0 Td")
            stream_ops.append(f"({qty_val:g}) Tj")
            stream_ops.append("50 0 Td")
            stream_ops.append(f"({unit_p:,.2f}) Tj")
            stream_ops.append("75 0 Td")
            stream_ops.append(f"({line_tot:,.2f}) Tj")
            stream_ops.append("ET")

            # Subtle alternating line border
            stream_ops.append("q")
            stream_ops.append("0.90 0.90 0.90 RG 0.5 w")
            stream_ops.append(f"50 {y_pos - 4} 495 0 re S")
            stream_ops.append("Q")

            y_pos -= 18
    else:
        # Fallback single summary line
        stream_ops.append("BT")
        stream_ops.append("/F2 9 Tf")
        stream_ops.append("0.2 0.2 0.2 rg")
        stream_ops.append(f"55 {y_pos} Td")
        stream_ops.append("(L01) Tj")
        stream_ops.append("80 0 Td")
        stream_ops.append(
            f"(Comprehensive AV Hardware & Engineering Services Package - {_escape_pdf_text(quote_id)}) Tj"
        )
        stream_ops.append("230 0 Td")
        stream_ops.append("(1) Tj")
        stream_ops.append("50 0 Td")
        stream_ops.append(f"({total_val:,.2f}) Tj")
        stream_ops.append("75 0 Td")
        stream_ops.append(f"({total_val:,.2f}) Tj")
        stream_ops.append("ET")

        stream_ops.append("q")
        stream_ops.append("0.90 0.90 0.90 RG 0.5 w")
        stream_ops.append(f"50 {y_pos - 4} 495 0 re S")
        stream_ops.append("Q")

        y_pos -= 18

    # 5. Attestation & Cryptographic Anchor Block (Bottom of page)
    attest_y = 170
    stream_ops.append("q")
    stream_ops.append("0.95 0.95 0.95 rg")
    stream_ops.append(f"50 {attest_y - 20} 495 65 re f")
    stream_ops.append("0.75 0.75 0.75 RG 0.75 w")
    stream_ops.append(f"50 {attest_y - 20} 495 65 re S")
    stream_ops.append("Q")

    stream_ops.append("BT")
    stream_ops.append("/F1 9 Tf")
    stream_ops.append("0.2 0.2 0.2 rg")
    stream_ops.append(f"60 {attest_y + 30} Td")
    stream_ops.append("(LEGAL & CRYPTOGRAPHIC ATTESTATION OF FROZEN BASELINE) Tj")
    stream_ops.append("/F2 8 Tf")
    stream_ops.append("0.35 0.35 0.35 rg")
    stream_ops.append("0 -13 Td")
    stream_ops.append(
        "(This document was deterministically generated by NCE from the immutable baseline record.) Tj"
    )
    stream_ops.append("0 -11 Td")
    stream_ops.append(
        "(The SHA-256 fingerprint of these exact bytes is recorded in the signing ceremony evidence trail.) Tj"
    )
    stream_ops.append("0 -11 Td")
    stream_ops.append(
        "(Any alteration of content, pricing, or commercial terms invalidates the attestation hash.) Tj"
    )
    stream_ops.append("ET")

    # Signature line demarcation
    stream_ops.append("BT")
    stream_ops.append("/F1 9 Tf")
    stream_ops.append("0 0 0 rg")
    stream_ops.append("50 85 Td")
    stream_ops.append(f"(Agreed and accepted for: {_escape_pdf_text(customer_name)}) Tj")
    stream_ops.append("320 0 Td")
    stream_ops.append("(E-Signature Ceremony Reference:) Tj")
    stream_ops.append("ET")

    stream_ops.append("q")
    stream_ops.append("0.3 0.3 0.3 RG 1 w")
    stream_ops.append("50 50 200 0 re S")  # Signature line
    stream_ops.append("320 50 220 0 re S")  # Ceremony ref line
    stream_ops.append("Q")

    stream_ops.append("BT")
    stream_ops.append("/F2 8 Tf")
    stream_ops.append("0.4 0.4 0.4 rg")
    stream_ops.append("50 38 Td")
    stream_ops.append(f"(Authorized Signer: {_escape_pdf_text(signer_name)}) Tj")
    stream_ops.append("320 0 Td")
    stream_ops.append(
        f"(Doc Anchor: {_escape_pdf_text(quote_id)} / {_escape_pdf_text(frozen_iso)}) Tj"
    )
    stream_ops.append("ET")

    # Encode content stream
    content_stream = "\n".join(stream_ops).encode("latin-1")
    stream_len = len(content_stream)

    # Deterministic PDF Object Graph
    # 1: Catalog
    # 2: Pages
    # 3: Page
    # 4: Content Stream
    # 5: Font F1 (Helvetica-Bold)
    # 6: Font F2 (Helvetica)
    objects: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595.28 841.89] "
            b"/Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>\n"
            b"endobj\n"
        ),
        (
            f"4 0 obj\n<< /Length {stream_len} >>\nstream\n".encode("latin-1")
            + content_stream
            + b"\nendstream\nendobj\n"
        ),
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n",
        b"6 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]

    # Deterministic Document ID derived from baseline natural key and timestamp
    doc_id_hex = hashlib.sha256(f"{quote_id}:{frozen_iso}:{total_val:.2f}".encode()).hexdigest()[
        :32
    ]

    # Write PDF stream with cross-reference table and trailer
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    offsets: list[int] = [0]
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)

    xref_offset = out.tell()
    out.write(f"xref\n0 {len(offsets)}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("latin-1"))

    trailer = (
        f"trailer\n"
        f"<< /Size {len(offsets)} /Root 1 0 R "
        f"/ID [<{doc_id_hex}> <{doc_id_hex}>] >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin-1")
    out.write(trailer)

    return out.getvalue()
