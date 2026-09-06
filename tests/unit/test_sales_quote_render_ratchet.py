"""
tests/unit/test_sales_quote_render_ratchet.py
=============================================
Wave Q-1 Ratchet & Acceptance Tests:
  - Deterministic quote PDF renderer from frozen baseline.
  - Hardened signature request orchestration rejecting dummy defaults.
  - Typed domain error mapping to 4xx (-32602 Invalid parameters), never -32603.
"""

from __future__ import annotations

import hashlib
import io
import json
from unittest.mock import AsyncMock, MagicMock

import pypdf
import pytest

from nce.mcp_errors import MCP_INVALID_PARAMS, McpError
from nce.vertical_modules.sales.mcp_handlers import handle_sales_request_signature
from nce.vertical_modules.sales.render import render_quote_document
from nce.vertical_modules.sales.signing import (
    MissingBaselineError,
    MissingSignerError,
    do_request_signature,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_QUOTE_ID = "quote-q1-1001"


# ---------------------------------------------------------------------------
# 1. Deterministic Rendering Tests
# ---------------------------------------------------------------------------


def test_render_quote_document_determinism():
    """Verify identical baseline and quote parameters produce byte-identical PDF bytes."""
    baseline = {
        "quote_id": _QUOTE_ID,
        "signed_total_nok": 420000.0,
        "signed_margin_pct": 0.38,
        "signed_at": "2026-06-24T12:00:00+00:00",
    }
    quote_details = {
        "name": "Auditorium Sound & Vision",
        "customer_name": "Nordic Solutions AS",
        "currency": "NOK",
    }
    line_items = [
        {
            "line_ref": "L01",
            "item_name": "Main PA Speakers",
            "qty": 2,
            "unit_price": 85000.0,
            "line_total": 170000.0,
        },
        {
            "line_ref": "L02",
            "item_name": "Power Amplifier 4x2500W",
            "qty": 2,
            "unit_price": 65000.0,
            "line_total": 130000.0,
        },
        {
            "line_ref": "L03",
            "item_name": "Digital Audio Processor",
            "qty": 1,
            "unit_price": 120000.0,
            "line_total": 120000.0,
        },
    ]
    signer = {
        "name": "Kari Nordmann",
        "email": "kari.nordmann@nordicsolutions.no",
    }

    pdf_run1 = render_quote_document(
        baseline=baseline,
        quote_details=quote_details,
        line_items=line_items,
        signer=signer,
    )
    pdf_run2 = render_quote_document(
        baseline=baseline,
        quote_details=quote_details,
        line_items=line_items,
        signer=signer,
    )

    # Byte-identical assertion
    assert pdf_run1 == pdf_run2
    h1 = hashlib.sha256(pdf_run1).hexdigest()
    h2 = hashlib.sha256(pdf_run2).hexdigest()
    assert h1 == h2


def test_render_quote_document_valid_pdf_structure():
    """Verify rendered bytes form a valid PDF 1.4 parseable by pypdf with expected text."""
    baseline = {
        "quote_id": _QUOTE_ID,
        "signed_total_nok": 150000.0,
        "signed_margin_pct": 0.28,
        "signed_at": "2026-07-01T09:30:00+00:00",
    }
    quote_details = {
        "name": "Executive Boardroom Video Bar",
        "customer_name": "Bergen Shipping Group",
        "currency": "NOK",
    }
    signer = {
        "name": "Per Hansen",
        "email": "per.hansen@bergenshipping.no",
    }

    pdf_bytes = render_quote_document(
        baseline=baseline,
        quote_details=quote_details,
        signer=signer,
    )

    # Assert PDF header and trailer
    assert pdf_bytes.startswith(b"%PDF-1.4")
    assert b"%%EOF" in pdf_bytes

    # Parse with pypdf
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1

    extracted = reader.pages[0].extract_text()
    assert "Executive Boardroom Video Bar" in extracted
    assert _QUOTE_ID in extracted
    assert "Bergen Shipping Group" in extracted
    assert "2026-07-01T09:30:00+00:00" in extracted
    assert "150,000.00 NOK" in extracted
    assert "28.0%" in extracted
    assert "Per Hansen" in extracted
    assert "per.hansen@bergenshipping.no" in extracted
    assert "LEGAL & CRYPTOGRAPHIC ATTESTATION OF FROZEN BASELINE" in extracted


def test_render_quote_document_missing_baseline_args():
    """Verify render_quote_document validates required baseline keys."""
    with pytest.raises(ValueError, match="baseline dict is required"):
        render_quote_document(baseline={})

    with pytest.raises(ValueError, match="baseline must contain a non-empty quote_id"):
        render_quote_document(baseline={"quote_id": ""})

    with pytest.raises(ValueError, match="baseline must contain signed_total_nok"):
        render_quote_document(baseline={"quote_id": "q1"})

    with pytest.raises(ValueError, match="baseline must contain signed_margin_pct"):
        render_quote_document(baseline={"quote_id": "q1", "signed_total_nok": 100})

    with pytest.raises(ValueError, match="baseline must contain signed_at timestamp"):
        render_quote_document(
            baseline={"quote_id": "q1", "signed_total_nok": 100, "signed_margin_pct": 0.3}
        )


# ---------------------------------------------------------------------------
# 2. Signing Core Refusals & Error Mapping Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_request_signature_rejects_caller_doc_bytes():
    """Verify caller is strictly prohibited from supplying doc_bytes (ruling b)."""
    engine = MagicMock()
    with pytest.raises(ValueError, match="doc_bytes parameter is not allowed"):
        await do_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "doc_bytes": b"Forged or Custom Document",
                "signer": {"name": "Alice", "email": "alice@acme.com"},
            },
        )


@pytest.mark.asyncio
async def test_do_request_signature_rejects_missing_signer():
    """Verify missing signer or placeholder signer raises MissingSignerError (mapped to 4xx)."""
    engine = MagicMock()

    # Missing signer dict entirely
    with pytest.raises(MissingSignerError, match="signer dict with 'name' and 'email' is required"):
        await do_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
            },
        )

    # Empty name
    with pytest.raises(MissingSignerError, match="signer 'name' is required"):
        await do_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "signer": {"name": "", "email": "alice@acme.com"},
            },
        )

    # Invalid email
    with pytest.raises(
        MissingSignerError, match="signer 'email' is required and must be a valid email"
    ):
        await do_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "signer": {"name": "Alice", "email": "not-an-email"},
            },
        )

    # Subclass contract
    assert issubclass(MissingSignerError, ValueError)


@pytest.mark.asyncio
async def test_do_request_signature_rejects_missing_baseline(monkeypatch):
    """Verify quote without a frozen baseline raises MissingBaselineError (never -32603)."""
    engine = MagicMock()

    # Mock DB row in sales_read_model
    quote_row = {
        "name": "Test Quote",
        "manual": {},
        "source_json": {"margin": 0.35, "total_price": 200000.0},
    }

    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = quote_row

    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_conn
    fake_cm.__aexit__.return_value = None

    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session",
        lambda _pool, _ns: fake_cm,
    )
    # Baseline does NOT exist in sales_signed_baselines
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=None),
    )

    with pytest.raises(MissingBaselineError, match="has no frozen baseline"):
        await do_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "signer": {"name": "Alice Signer", "email": "alice@acme.com"},
            },
        )

    # Subclass contract
    assert issubclass(MissingBaselineError, ValueError)


@pytest.mark.asyncio
async def test_mcp_handler_maps_missing_baseline_and_signer_to_4xx(monkeypatch):
    """Verify @mcp_handler catches MissingBaselineError and MissingSignerError as MCP_INVALID_PARAMS (-32602)."""
    engine = MagicMock()

    # 1. Missing signer test through MCP handler
    with pytest.raises(McpError) as exc_info:
        await handle_sales_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                # No signer provided
            },
        )
    assert exc_info.value.code == MCP_INVALID_PARAMS
    assert exc_info.value.code != -32603

    # 2. Missing baseline test through MCP handler
    quote_row = {
        "name": "Test Quote",
        "manual": {},
        "source_json": {"margin": 0.35, "total_price": 200000.0},
    }
    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = quote_row
    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_conn
    fake_cm.__aexit__.return_value = None

    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session",
        lambda _pool, _ns: fake_cm,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=None),
    )

    with pytest.raises(McpError) as exc_info:
        await handle_sales_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "signer": {"name": "Alice Signer", "email": "alice@acme.com"},
            },
        )
    assert exc_info.value.code == MCP_INVALID_PARAMS
    assert exc_info.value.code != -32603

    # 3. Supplying doc_bytes rejected through MCP handler
    with pytest.raises(McpError) as exc_info:
        await handle_sales_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "doc_bytes": "Arbitrary Bytes",
                "signer": {"name": "Alice Signer", "email": "alice@acme.com"},
            },
        )
    assert exc_info.value.code == MCP_INVALID_PARAMS


# ---------------------------------------------------------------------------
# 3. Happy Path: End-to-End Signature Request with Frozen Baseline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_request_signature_happy_path(monkeypatch):
    """Verify happy path renders deterministic PDF, computes document_hash, and updates read model."""
    engine = MagicMock()

    quote_row = {
        "name": "Conference Room AV Overhaul",
        "manual": {},
        "source_json": {
            "customer_name": "Telenor Norge AS",
            "currency": "NOK",
            "margin": 0.32,
            "total_price": 280000.0,
        },
    }
    baseline_row = {
        "id": "1",
        "quote_id": _QUOTE_ID,
        "signed_margin_pct": 0.32,
        "signed_total_nok": 280000.0,
        "signed_at": "2026-06-25T10:00:00+00:00",
    }
    bom_lines = [
        {
            "line_ref": "L01",
            "item_name": "Yealink MeetingBar A30",
            "qty": 1,
            "unit_price": 35000.0,
            "line_total": 35000.0,
        },
        {
            "line_ref": "L02",
            "item_name": "Neat Board 65",
            "qty": 1,
            "unit_price": 65000.0,
            "line_total": 65000.0,
        },
    ]

    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = quote_row
    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_conn
    fake_cm.__aexit__.return_value = None

    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.scoped_pg_session",
        lambda _pool, _ns: fake_cm,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.get_signed_baseline",
        AsyncMock(return_value=baseline_row),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote",
        AsyncMock(return_value=bom_lines),
    )

    session = await do_request_signature(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "quote_id": _QUOTE_ID,
            "signer": {"name": "Jonas Lie", "email": "jonas.lie@telenor.no"},
            "method": "manual",
        },
    )

    assert session["status"] == "pending"
    assert session["session_id"] is not None
    assert "document_hash" in session
    assert session["fingerprint"] == session["document_hash"]
    assert session["quote_id"] == _QUOTE_ID

    # Verify database update
    fake_conn.execute.assert_called_once()
    update_sql, manual_json, _ns_str, _q_str = fake_conn.execute.call_args[0]
    assert "UPDATE sales_read_model" in update_sql
    saved_manual = json.loads(manual_json)
    assert saved_manual["signing_status"] == "pending"
    assert saved_manual["signing_session_id"] == session["session_id"]
    assert saved_manual["document_hash"] == session["document_hash"]
    assert saved_manual["signer_name"] == "Jonas Lie"
    assert saved_manual["signer_email"] == "jonas.lie@telenor.no"
