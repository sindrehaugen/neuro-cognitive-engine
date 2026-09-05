"""Unit tests for Module 17 Customer Portal Engine: Post-Handover Surfaces (Phase 3).

Charter Phase 3 Gates:
  1. Document shares: Scoped to customer, expiry enforced, revoked excluded, internal fields stripped.
  2. Single document get: Expired/revoked grant access refused (PermissionError).
  3. SLA self-service: Agreements terms + Support running clock, strictly stripping MRR, contract value, and penalty amounts.
  4. Invoices: Customer invoice listing, strictly stripping internal margin, our cost, and rebate percentages.
  5. Graceful degradation: Each surface degrades cleanly when owner engine / data is absent.
  6. IDOR refusal: Customer A cannot access Customer B's documents, SLA, or invoices.
  7. HTTP REST routes on customer portal app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from starlette.testclient import TestClient

from nce.vertical_modules.customer_portal.app import build_customer_portal_app
from nce.vertical_modules.customer_portal.documents import (
    do_get_document,
    do_list_documents,
)
from nce.vertical_modules.customer_portal.invoices import do_list_invoices
from nce.vertical_modules.customer_portal.sla import do_sla_status

REPO_ROOT = Path(__file__).resolve().parents[2]

_NAMESPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
_CUSTOMER_A_SCOPE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_CUSTOMER_B_SCOPE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@pytest.mark.asyncio
async def test_documents_list_and_expiry_filtering():
    """do_list_documents must filter out expired or revoked document shares and strip commercial fields."""
    mock_engine = None
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=30)).isoformat()
    past = (now - timedelta(days=5)).isoformat()

    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "documents": [
            {
                "share_id": "share-active-fdv",
                "document_ref": "doc-fdv-001.pdf",
                "document_kind": "fdv",
                "title": "Operation & Maintenance Manual",
                "expires_at": future,
                "created_at": "2026-08-01T10:00:00Z",
                # Forbidden fields
                "internal_cost": 1500.00,
                "markup": 0.20,
                "margin": 0.15,
                "supplier_notes": "Internal contractor markup",
            },
            {
                "share_id": "share-expired-sow",
                "document_ref": "doc-sow-legacy.pdf",
                "document_kind": "sow",
                "title": "Expired Scope of Work",
                "expires_at": past,  # Expired
                "created_at": "2025-01-01T10:00:00Z",
            },
            {
                "share_id": "share-revoked-drawing",
                "document_ref": "doc-dwg-revoked.pdf",
                "document_kind": "drawing",
                "title": "Revoked Schematic",
                "expires_at": future,
                "revoked_at": "2026-08-10T12:00:00Z",  # Revoked
                "created_at": "2026-07-01T10:00:00Z",
            },
        ],
    }

    result = await do_list_documents(mock_engine, params)

    assert result["customer_scope_id"] == str(_CUSTOMER_A_SCOPE)
    assert result["total_documents"] == 1
    docs = result["documents"]
    assert len(docs) == 1

    doc = docs[0]
    assert doc["share_id"] == "share-active-fdv"
    assert doc["title"] == "Operation & Maintenance Manual"
    assert doc["document_kind"] == "fdv"

    # Strictly forbidden fields check
    for forbidden in ("internal_cost", "markup", "margin", "supplier_notes"):
        assert forbidden not in doc, f"Forbidden field {forbidden!r} leaked in document share!"


@pytest.mark.asyncio
async def test_documents_get_single_and_expired_refusal():
    """do_get_document retrieves active share and refuses expired/revoked share with PermissionError."""
    mock_engine = None
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=15)).isoformat()
    past = (now - timedelta(days=2)).isoformat()

    # 1. Active document
    active_params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "share_id": "share-valid-asbuilt",
        "document": {
            "share_id": "share-valid-asbuilt",
            "document_ref": "as-built-drawing.pdf",
            "document_kind": "as_built",
            "title": "As-Built System Schematic",
            "expires_at": future,
            "created_at": "2026-08-20T09:00:00Z",
            "margin": 0.25,
        },
    }
    doc = await do_get_document(mock_engine, active_params)
    assert doc["share_id"] == "share-valid-asbuilt"
    assert doc["document_kind"] == "as_built"
    assert "margin" not in doc

    # 2. Expired document -> PermissionError
    expired_params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "share_id": "share-old",
        "document": {
            "share_id": "share-old",
            "document_ref": "old.pdf",
            "expires_at": past,
        },
    }
    with pytest.raises(PermissionError, match="expired"):
        await do_get_document(mock_engine, expired_params)

    # 3. Revoked document -> PermissionError
    revoked_params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "share_id": "share-revoked",
        "document": {
            "share_id": "share-revoked",
            "document_ref": "revoked.pdf",
            "expires_at": future,
            "revoked_at": "2026-09-01T00:00:00Z",
        },
    }
    with pytest.raises(PermissionError, match="revoked"):
        await do_get_document(mock_engine, revoked_params)


@pytest.mark.asyncio
async def test_sla_status_allowlist_redaction_and_graceful_degradation():
    """do_sla_status returns customer terms and clock, suppressing MRR, contract value, and penalty amounts."""
    mock_engine = None

    # Scenario A: Full SLA data present
    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "sla_id": "sla-gold-001",
        "tier_name": "Gold 24/7 Response",
        "response_target_hours": 2,
        "resolution_target_hours": 8,
        "current_clock_hours": 1.5,
        "is_breached": False,
        "period_start": "2026-09-01T00:00:00Z",
        "period_end": "2026-09-30T23:59:59Z",
        # Toxic fields that must be redacted:
        "contract_value": 450000.00,
        "mrr": 37500.00,
        "penalty_amount": 0.00,
        "internal_cost": 12000.00,
    }

    result = await do_sla_status(mock_engine, params)

    assert result["sla_id"] == "sla-gold-001"
    assert result["tier_name"] == "Gold 24/7 Response"
    assert result["response_target_hours"] == 2
    assert result["is_breached"] is False

    for forbidden in ("contract_value", "mrr", "penalty_amount", "internal_cost"):
        assert forbidden not in result, f"Forbidden SLA field {forbidden!r} leaked!"

    # Scenario B: Graceful degradation when upstream is absent
    minimal_params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
    }
    fallback = await do_sla_status(mock_engine, minimal_params)
    assert fallback["tier_name"] == "Standard SLA"
    assert fallback["is_breached"] is False
    assert fallback["current_clock_hours"] == 0.0


@pytest.mark.asyncio
async def test_invoices_allowlist_redaction_and_graceful_degradation():
    """do_list_invoices returns customer invoices, stripping internal margin, our_cost, and rebate."""
    mock_engine = None

    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "invoices": [
            {
                "invoice_id": "inv-2026-0801",
                "invoice_number": "INV-10042",
                "amount_ex_vat": 80000.00,
                "vat_amount": 20000.00,
                "amount_inc_vat": 100000.00,
                "currency": "NOK",
                "due_date": "2026-09-15",
                "status": "issued",
                "issued_at": "2026-08-15T08:00:00Z",
                # Toxic commercial fields:
                "internal_margin": 0.32,
                "our_cost": 54400.00,
                "rebate_percentage": 0.05,
            }
        ],
    }

    result = await do_list_invoices(mock_engine, params)

    assert result["customer_scope_id"] == str(_CUSTOMER_A_SCOPE)
    assert result["total_invoices"] == 1
    invoices = result["invoices"]
    assert len(invoices) == 1

    inv = invoices[0]
    assert inv["invoice_number"] == "INV-10042"
    assert inv["amount_inc_vat"] == 100000.00
    assert inv["currency"] == "NOK"

    for forbidden in ("internal_margin", "our_cost", "rebate_percentage"):
        assert forbidden not in inv, f"Forbidden invoice field {forbidden!r} leaked!"

    # Graceful degradation with empty upstream
    empty_result = await do_list_invoices(
        mock_engine, {"customer_scope_id": str(_CUSTOMER_A_SCOPE)}
    )
    assert empty_result["total_invoices"] == 0
    assert empty_result["invoices"] == []


@pytest.mark.asyncio
async def test_customer_scope_idor_refusal_post_handover():
    """Mismatched customer scope IDOR attempts must raise PermissionError."""
    mock_engine = None

    idor_params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "target_scope_id": str(_CUSTOMER_B_SCOPE),
    }

    with pytest.raises(PermissionError, match="IDOR"):
        await do_list_documents(mock_engine, idor_params)

    with pytest.raises(PermissionError, match="IDOR"):
        await do_get_document(mock_engine, {**idor_params, "share_id": "share-secret"})

    with pytest.raises(PermissionError, match="IDOR"):
        await do_sla_status(mock_engine, idor_params)

    with pytest.raises(PermissionError, match="IDOR"):
        await do_list_invoices(mock_engine, idor_params)


def test_portal_app_post_handover_endpoints():
    """Verify HTTP REST routes for documents, SLA, and invoices on customer portal app."""
    app = build_customer_portal_app()
    client = TestClient(app)

    headers = {
        "X-Customer-Scope-ID": str(_CUSTOMER_A_SCOPE),
        "X-Namespace-ID": str(_NAMESPACE_ID),
    }

    # 1. Documents list
    resp = client.get("/api/portal/documents", headers=headers)
    assert resp.status_code == 200
    assert "documents" in resp.json()

    # 2. SLA status
    resp = client.get("/api/portal/sla", headers=headers)
    assert resp.status_code == 200
    assert "tier_name" in resp.json()

    # 3. Invoices list
    resp = client.get("/api/portal/invoices", headers=headers)
    assert resp.status_code == 200
    assert "invoices" in resp.json()
