"""Ratchet & acceptance unit tests for Sales Quote Signing with EmailCodeTransport (Wave Q-2).

Covers:
  1. do_request_signature with method="email_code" generates deterministic PDF from frozen baseline,
     dispatches OTP code, and stores signing_method and signing_security_tier in read model.
  2. do_on_signed_callback with valid OTP verifies code, freezes baseline, and converts to project.
  3. do_on_signed_callback with invalid OTP fails closed, freezes no baseline, converts no project.
  4. do_request_signature refuses unbuilt transports ("oneflow", "criipto", "signicat") with
     UnimplementedTransportError (never silently downgrading to manual).
  5. handle_sales_request_signature MCP handler maps UnimplementedTransportError to 4xx (-32602).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.mcp_errors import MCP_INVALID_PARAMS, McpError
from nce.signing_service import (
    UnimplementedTransportError,
    clear_dispatched_otp_codes,
    get_dispatched_otp_codes,
    reset_signing_transports,
)
from nce.vertical_modules.sales.mcp_handlers import handle_sales_request_signature
from nce.vertical_modules.sales.signing import (
    do_on_signed_callback,
    do_request_signature,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_QUOTE_ID = "quote-email-q2-1001"


@pytest.fixture(autouse=True)
def _isolate_signing():
    clear_dispatched_otp_codes()
    reset_signing_transports()
    yield
    clear_dispatched_otp_codes()
    reset_signing_transports()


# ---------------------------------------------------------------------------
# 1. Unbuilt Vendor Transports Fail Loud (No Silent Downgrade)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_request_signature_refuses_unbuilt_vendor_transports():
    """Requesting signature with unbuilt vendors raises UnimplementedTransportError (NotImplementedError)."""
    engine = MagicMock()

    for vendor in ("oneflow", "criipto", "signicat"):
        with pytest.raises(UnimplementedTransportError) as exc_info:
            await do_request_signature(
                engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "quote_id": _QUOTE_ID,
                    "signer": {"name": "Bob Signer", "email": "bob@acme.no"},
                    "method": vendor,
                },
            )
        assert isinstance(exc_info.value, NotImplementedError)
        assert isinstance(exc_info.value, ValueError)
        assert vendor in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_handler_maps_unimplemented_transport_to_4xx():
    """handle_sales_request_signature maps UnimplementedTransportError to -32602 (4xx), never -32603."""
    engine = MagicMock()

    with pytest.raises(McpError) as exc_info:
        await handle_sales_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": _QUOTE_ID,
                "signer": {"name": "Bob Signer", "email": "bob@acme.no"},
                "method": "oneflow",
            },
        )
    assert exc_info.value.code == MCP_INVALID_PARAMS
    assert exc_info.value.code != -32603


# ---------------------------------------------------------------------------
# 2. End-to-End E-Signature Request via email_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_request_signature_email_code_happy_path(monkeypatch):
    """Verify email_code signing renders PDF, records security tier, and dispatches OTP."""
    engine = MagicMock()

    quote_row = {
        "name": "Boardroom VC Infrastructure",
        "manual": {},
        "source_json": {
            "customer_name": "Equinor ASA",
            "currency": "NOK",
            "margin": 0.35,
            "total_price": 450000.0,
        },
    }
    baseline_row = {
        "id": "1",
        "quote_id": _QUOTE_ID,
        "signed_margin_pct": 0.35,
        "signed_total_nok": 450000.0,
        "signed_at": "2026-07-01T12:00:00+00:00",
    }
    bom_lines = [
        {
            "line_ref": "L01",
            "item_name": "Neat Bar Pro",
            "qty": 2,
            "unit_price": 50000.0,
            "line_total": 100000.0,
        }
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
            "signer": {"name": "Lise Hansen", "email": "lise.hansen@equinor.com"},
            "method": "email_code",
        },
    )

    assert session["status"] == "pending"
    assert session["method"] == "email_code"
    assert session["security_tier"] == "email_code"
    assert "document_hash" in session
    assert session["quote_id"] == _QUOTE_ID

    # Dispatched code check
    dispatched = get_dispatched_otp_codes()
    assert len(dispatched) == 1
    assert dispatched[0]["email"] == "lise.hansen@equinor.com"
    otp_code = dispatched[0]["code"]
    assert len(otp_code) == 6

    # Verify DB write
    fake_conn.execute.assert_called_once()
    update_sql, manual_json, _ns_str, _q_str = fake_conn.execute.call_args[0]
    assert "UPDATE sales_read_model" in update_sql
    saved_manual = json.loads(manual_json)
    assert saved_manual["signing_status"] == "pending"
    assert saved_manual["signing_method"] == "email_code"
    assert saved_manual["signing_security_tier"] == "email_code"
    assert saved_manual["document_hash"] == session["document_hash"]
    assert saved_manual["signer_email"] == "lise.hansen@equinor.com"


# ---------------------------------------------------------------------------
# 3. OTP Verification & Signed Callback via email_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_on_signed_callback_email_code_success(monkeypatch):
    """do_on_signed_callback with valid OTP verifies code, freezes baseline, and triggers conversion."""
    engine = MagicMock()

    # Step 1: Setup request session
    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "Auditorium System",
        "manual": {},
        "source_json": {
            "customer_name": "Hydro ASA",
            "currency": "NOK",
            "margin": 0.40,
            "total_price": 800000.0,
        },
    }
    baseline_row = {
        "id": "1",
        "quote_id": _QUOTE_ID,
        "signed_margin_pct": 0.40,
        "signed_total_nok": 800000.0,
        "signed_at": "2026-07-02T10:00:00+00:00",
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
        AsyncMock(return_value=baseline_row),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote",
        AsyncMock(return_value=[]),
    )

    req_session = await do_request_signature(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "quote_id": _QUOTE_ID,
            "signer": {"name": "Lise Hansen", "email": "lise.hansen@hydro.com"},
            "method": "email_code",
        },
    )
    session_id = req_session["session_id"]
    otp_code = get_dispatched_otp_codes()[0]["code"]

    # Step 2: Callback with valid OTP
    mock_freeze = AsyncMock(return_value={"ok": True, "baseline_id": "b-101"})
    mock_convert = AsyncMock(return_value={"ok": True, "project_id": "PROJECT:QUOTE-EMAIL-Q2-1001"})

    monkeypatch.setattr("nce.vertical_modules.sales.signing.do_freeze_baseline", mock_freeze)
    monkeypatch.setattr("nce.vertical_modules.sales.signing.do_convert_signed_quote", mock_convert)

    # In read model, the session is pending
    quote_row["manual"] = {
        "signing_session_id": session_id,
        "signing_status": "pending",
        "signing_method": "email_code",
        "signing_security_tier": "email_code",
        "signer_name": "Lise Hansen",
        "signer_email": "lise.hansen@hydro.com",
    }

    res = await do_on_signed_callback(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "session_id": session_id,
            "callback_payload": {"code": otp_code},
        },
    )

    assert res["ok"] is True
    assert res["baseline_frozen"] is True
    assert res["project_id"] == "PROJECT:QUOTE-EMAIL-Q2-1001"
    assert res["security_tier"] == "email_code"
    mock_freeze.assert_called_once()
    mock_convert.assert_called_once()


@pytest.mark.asyncio
async def test_do_on_signed_callback_email_code_wrong_code_fails_closed(monkeypatch):
    """do_on_signed_callback with wrong OTP fails closed, freezes no baseline, converts no project."""
    engine = MagicMock()

    quote_row = {
        "source_id": _QUOTE_ID,
        "name": "Auditorium System",
        "manual": {},
        "source_json": {
            "customer_name": "Hydro ASA",
            "currency": "NOK",
            "margin": 0.40,
            "total_price": 800000.0,
        },
    }
    baseline_row = {
        "id": "1",
        "quote_id": _QUOTE_ID,
        "signed_margin_pct": 0.40,
        "signed_total_nok": 800000.0,
        "signed_at": "2026-07-02T10:00:00+00:00",
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
        AsyncMock(return_value=baseline_row),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.sales.signing.list_bom_lines_for_quote",
        AsyncMock(return_value=[]),
    )

    req_session = await do_request_signature(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "quote_id": _QUOTE_ID,
            "signer": {"name": "Lise Hansen", "email": "lise.hansen@hydro.com"},
            "method": "email_code",
        },
    )
    session_id = req_session["session_id"]

    mock_freeze = AsyncMock()
    mock_convert = AsyncMock()
    monkeypatch.setattr("nce.vertical_modules.sales.signing.do_freeze_baseline", mock_freeze)
    monkeypatch.setattr("nce.vertical_modules.sales.signing.do_convert_signed_quote", mock_convert)

    quote_row["manual"] = {
        "signing_session_id": session_id,
        "signing_status": "pending",
        "signing_method": "email_code",
        "signing_security_tier": "email_code",
    }

    # Wrong candidate code
    with pytest.raises(ValueError, match="Invalid verification code"):
        await do_on_signed_callback(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "session_id": session_id,
                "callback_payload": {"code": "000000"},
            },
        )

    # Assert baseline was NOT frozen and project was NOT converted
    mock_freeze.assert_not_called()
    mock_convert.assert_not_called()
