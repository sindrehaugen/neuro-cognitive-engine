"""Unit tests for Module 17 Customer Portal Engine: Inbound Customer Actions & Hand-offs (Phase 4).

Charter Phase 4 Gates:
  1. do_raise_service_request: Inbound request intake -> Support do_open_ticket hand-off.
  2. Idempotency: Duplicate request_id never creates multiple tickets or duplicate intakes.
  3. Contract-B gating: Billable out-of-scope requests require authorization or are refused when unentitled.
  4. Two-master customer status projection: Neutral mapping (never exposes internal ticket churn/escalation).
  5. do_register_expansion_interest: Inbound re-buy interest hands off to Sales lead queue (human-gated).
  6. IDOR refusal: Customer A cannot raise service request or register expansion for Customer B.
  7. HTTP REST routes on customer portal app.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from starlette.testclient import TestClient

from nce.vertical_modules.customer_portal.actions import (
    do_raise_service_request,
    do_register_expansion_interest,
)
from nce.vertical_modules.customer_portal.app import build_customer_portal_app

REPO_ROOT = Path(__file__).resolve().parents[2]

_NAMESPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
_CUSTOMER_A_SCOPE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_CUSTOMER_B_SCOPE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@pytest.mark.asyncio
async def test_raise_service_request_and_support_handoff():
    """do_raise_service_request records intake and hands off to Support with safe status projection."""
    mock_engine = None

    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "request_id": "req-2026-001",
        "room_id": "room-alpha-boardroom",
        "summary": "Audio cutting out on left ceiling microphone array.",
        "urgency": "normal",
        "contract_b_covered": True,
        # Raw internal fields that might be passed from internal system:
        "internal_priority": "P1_CRITICAL",
        "assigned_internal_tech": "tech-erik-99",
        "internal_notes": "Customer is high-churn risk, expedite fix",
        "escalation_level": "LEVEL_3",
    }

    result = await do_raise_service_request(mock_engine, params)

    assert result["request_id"] == "req-2026-001"
    assert result["room_id"] == "room-alpha-boardroom"
    assert result["customer_status"] in ("received", "under_review")
    assert result["summary"] == "Audio cutting out on left ceiling microphone array."

    # Forbidden fields must be strictly redacted
    for forbidden in (
        "internal_priority",
        "assigned_internal_tech",
        "internal_notes",
        "escalation_level",
        "churn_risk",
    ):
        assert forbidden not in result, f"Internal field {forbidden!r} leaked in service request!"


@pytest.mark.asyncio
async def test_raise_service_request_idempotency():
    """Duplicate requests with the same request_id must return existing intake without double-dispatch."""
    mock_engine = None

    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "request_id": "req-idemp-duplicate",
        "room_id": "room-101",
        "summary": "Display will not power on from touch panel.",
        "contract_b_covered": True,
    }

    first = await do_raise_service_request(mock_engine, params)
    second = await do_raise_service_request(mock_engine, params)

    assert first["request_id"] == second["request_id"]
    assert first["created_at"] == second["created_at"]


@pytest.mark.asyncio
async def test_raise_service_request_contract_b_refusal():
    """Service request without contract coverage or spend pre-authorization is refused."""
    mock_engine = None

    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "request_id": "req-uncovered",
        "room_id": "room-uncovered-annex",
        "summary": "Requesting acoustic panel installation.",
        "contract_b_covered": False,
        "spend_authorized": False,
    }

    with pytest.raises(PermissionError, match="Contract-B|entitlement|spend authorization"):
        await do_raise_service_request(mock_engine, params)


@pytest.mark.asyncio
async def test_register_expansion_interest_and_sales_handoff():
    """do_register_expansion_interest records re-buy intent and surfaces lead to Sales."""
    mock_engine = None

    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "room_id": "room-alpha-boardroom",
        "category": "video_conferencing",
        "description": "Interested in adding dual PTZ cameras for hybrid meetings.",
        "estimated_users": 20,
    }

    result = await do_register_expansion_interest(mock_engine, params)

    assert result["status"] == "recorded"
    assert "interest_id" in result
    assert "sales" in result["message"].lower() or "advisory" in result["message"].lower()


@pytest.mark.asyncio
async def test_customer_scope_idor_refusal_actions():
    """Cross-customer actions must raise PermissionError."""
    mock_engine = None

    idor_params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "target_scope_id": str(_CUSTOMER_B_SCOPE),
        "request_id": "req-idor-test",
        "room_id": "room-b-secret",
        "summary": "Cross-customer intrusion attempt",
    }

    with pytest.raises(PermissionError, match="IDOR"):
        await do_raise_service_request(mock_engine, idor_params)

    with pytest.raises(PermissionError, match="IDOR"):
        await do_register_expansion_interest(mock_engine, idor_params)


def test_portal_app_actions_endpoints():
    """Verify HTTP POST endpoints for service requests and expansion interest."""
    app = build_customer_portal_app()
    client = TestClient(app)

    headers = {
        "X-Customer-Scope-ID": str(_CUSTOMER_A_SCOPE),
        "X-Namespace-ID": str(_NAMESPACE_ID),
    }

    # 1. Raise service request -> 200 OK
    resp = client.post(
        "/api/portal/service-requests",
        headers=headers,
        json={
            "request_id": "req-http-001",
            "room_id": "room-alpha",
            "summary": "Touch panel firmware unresponsive",
            "contract_b_covered": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["request_id"] == "req-http-001"

    # 2. Register expansion interest -> 200 OK
    resp = client.post(
        "/api/portal/expansion-interest",
        headers=headers,
        json={
            "room_id": "room-alpha",
            "category": "audio",
            "description": "Looking to upgrade ceiling speakers",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"
