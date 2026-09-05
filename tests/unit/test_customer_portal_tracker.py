"""Unit tests for Module 17 Customer Portal Engine: Room-Centric Read Projections & Messy Reality.

Charter Phase 2 Gates:
  1. Room tracker stages config contract (Domino's stages 10% -> 100%, BOM & Asset mappings).
  2. The FOUR messy reality cases render customer-safe:
     - Delay: neutral "In progress", zero internal slip/delay tags.
     - Change Order: neutral "Scope updated", never exposes regression or margin variance.
     - Partial Delivery: neutral "Partial delivery on site", proportional weighting.
     - Frozen/Stalled: neutral "Verification in progress", zero churn/defect fields.
  3. Room overview: %-ready rollup across functional locations, allow-list projected.
  4. Asset register: room-centric assets with model/warranty, strictly stripped of purchase/margin/supplier.
  5. Customer scope IDOR refusal: Customer A cannot access Customer B's room or assets.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from nce.vertical_modules.customer_portal.rooms import (
    compute_room_stage_and_progress,
    do_asset_register,
    do_room_overview,
    do_room_tracker,
    load_room_tracker_stages,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_NAMESPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
_CUSTOMER_A_SCOPE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_CUSTOMER_B_SCOPE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def test_room_tracker_stages_config_contract():
    """Verify room-tracker-stages.json adheres to schema and defines Domino's stages and messy rules."""
    config = load_room_tracker_stages()

    stages = config["stages"]
    stage_ids = [s["id"] for s in stages]
    assert stage_ids == ["planned", "ordered", "delivered", "installed", "tested", "ready"]

    # Monotonic weighting up to 100
    weights = [s["weight_pct"] for s in stages]
    assert weights == sorted(weights)
    assert weights[-1] == 100

    # Ensure 4 messy reality rules are defined
    messy_rules = config["messy_reality_rules"]
    for case in ("delay", "change_order", "partial_delivery", "frozen"):
        assert case in messy_rules, f"Messy reality rule for {case!r} missing"
        assert "customer_safe_status" in messy_rules[case]
        assert "customer_safe_narrative" in messy_rules[case]
        assert "redact_fields" in messy_rules[case]


def test_room_tracker_happy_path_dominos_progression():
    """Domino's tracker progresses monotonically based on BOM line and asset status."""
    # 1. Planned
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "planned"}, {"status": "approved"}],
        assets=[],
    )
    assert stage == "planned"
    assert pct == 10
    assert status == "Planned"

    # 2. Ordered
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "ordered"}, {"status": "ordered"}],
        assets=[],
    )
    assert stage == "ordered"
    assert pct == 25

    # 3. Delivered
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "received"}, {"status": "delivered"}],
        assets=[],
    )
    assert stage == "delivered"
    assert pct == 50

    # 4. Installed
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "installed"}],
        assets=[{"lifecycle": "installed"}],
    )
    assert stage == "installed"
    assert pct == 75

    # 5. Tested
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "tested"}],
        assets=[{"lifecycle": "commissioned"}],
    )
    assert stage == "tested"
    assert pct == 90

    # 6. Ready
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "operational"}],
        assets=[{"lifecycle": "active"}],
    )
    assert stage == "ready"
    assert pct == 100


def test_messy_reality_delay_renders_customer_safe():
    """Case 1 (Delay): Overdue / stalled delivery renders as neutral 'In progress' with internal slip stripped."""
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "ordered"}],
        assets=[],
        messy_context={
            "case": "delay",
            "internal_slip_days": 42,
            "delay_reason": "Vendor chip shortage",
            "supplier_delay": True,
            "escalation_level": "RED",
            "internal_notes": "Project manager escalating with distributor",
        },
    )
    assert status == "In progress"
    assert "in progress" in narrative.lower()
    # Confirm no toxic internal tokens in the narrative
    for toxic in ("shortage", "slip", "escalat", "vendor", "chip"):
        assert toxic not in narrative.lower(), f"Toxic leak in narrative: {toxic}"


def test_messy_reality_change_order_renders_scope_updated():
    """Case 2 (Change Order): BOM modification / return renders as neutral 'Scope updated', never regression."""
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "ordered"}],  # Reduced from earlier installed state
        assets=[],
        messy_context={
            "case": "change_order",
            "churn_type": "descope",
            "variance_cost": -15000.00,
            "dispute_flag": True,
            "change_order_margin_delta": -0.08,
        },
    )
    assert status == "Scope updated"
    assert "updated" in narrative.lower()
    for toxic in ("descope", "variance", "dispute", "margin", "churn"):
        assert toxic not in narrative.lower()


def test_messy_reality_partial_delivery_renders_staged():
    """Case 3 (Partial Delivery): Staggered equipment delivery renders as 'Partial delivery on site'."""
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "delivered"}, {"status": "ordered"}],
        assets=[],
        messy_context={
            "case": "partial_delivery",
            "backorder_flag": True,
            "supplier_stockout": True,
            "carrier_delay_code": "CUSTOMS_HOLD",
        },
    )
    assert status == "Partial delivery on site"
    assert "staged" in narrative.lower() or "transit" in narrative.lower()
    for toxic in ("backorder", "stockout", "customs", "hold"):
        assert toxic not in narrative.lower()


def test_messy_reality_frozen_upstream_renders_verification():
    """Case 4 (Frozen / Stalled): Room awaiting quality or site gate renders as 'Verification in progress'."""
    stage, pct, status, narrative = compute_room_stage_and_progress(
        bom_lines=[{"status": "ordered"}],
        assets=[],
        messy_context={
            "case": "frozen",
            "stalled_since": "2026-06-01",
            "defect_ticket_count": 5,
            "blocker_summary": "Subcontractor cabling rejected",
            "churn_risk_score": 0.85,
        },
    )
    assert status == "Verification in progress"
    assert "verification" in narrative.lower()
    for toxic in ("defect", "blocker", "stalled", "churn", "risk"):
        assert toxic not in narrative.lower()


@pytest.mark.asyncio
async def test_do_room_tracker_with_allowlist_redaction():
    """do_room_tracker produces customer-safe output conforming to room_tracker projection."""
    mock_engine = None  # in-memory projection
    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "room_id": "room-alpha-boardroom",
        "room_name": "Boardroom A",
        "site_id": "site-oslo-hq",
        "site_name": "Oslo HQ",
        "bom_lines": [
            {"id": "b1", "status": "installed", "cost": 4500.00, "margin": 0.35},
            {"id": "b2", "status": "delivered", "cost": 8900.00, "margin": 0.28},
        ],
        "assets": [
            {"id": "a1", "lifecycle": "installed", "purchase_price": 5000.00},
        ],
        "internal_slip": 14,
        "churn_risk": 0.40,
        "health_score": 62,
    }

    result = await do_room_tracker(mock_engine, params)

    assert result["room_id"] == "room-alpha-boardroom"
    assert result["room_name"] == "Boardroom A"
    assert result["stage"] in ["delivered", "installed"]
    assert 50 <= result["percent_ready"] <= 75

    # Strictly verify that forbidden internal fields were redacted
    for forbidden in (
        "cost",
        "margin",
        "purchase_price",
        "internal_slip",
        "churn_risk",
        "health_score",
        "supplier_terms",
    ):
        assert forbidden not in result, f"Forbidden field {forbidden!r} leaked in do_room_tracker!"


@pytest.mark.asyncio
async def test_do_room_overview_rollup_and_redaction():
    """do_room_overview returns %-ready rollup across customer rooms with strict allow-list."""
    mock_engine = None
    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "site_id": "site-oslo-hq",
        "site_name": "Oslo HQ",
        "rooms": [
            {
                "room_id": "room-101",
                "room_name": "Meeting Room 1",
                "bom_lines": [{"status": "operational"}],
                "margin": 0.30,
            },
            {
                "room_id": "room-102",
                "room_name": "Boardroom",
                "bom_lines": [{"status": "delivered"}],
                "internal_slip": 10,
            },
        ],
        "internal_project_id": "PROJ-SECRET-999",
        "margin": 0.45,
    }

    result = await do_room_overview(mock_engine, params)

    assert result["site_id"] == "site-oslo-hq"
    assert result["total_rooms"] == 2
    assert 70 <= result["overall_percent_ready"] <= 80
    assert len(result["rooms"]) == 2

    # Overview top-level redaction
    assert "internal_project_id" not in result
    assert "margin" not in result

    # Nested room redaction
    for room in result["rooms"]:
        assert "margin" not in room
        assert "internal_slip" not in room
        assert "room_id" in room
        assert "percent_ready" in room


@pytest.mark.asyncio
async def test_do_asset_register_room_centric_and_redaction():
    """do_asset_register returns room-centric equipment register with commercial fields redacted."""
    mock_engine = None
    params = {
        "namespace_id": str(_NAMESPACE_ID),
        "customer_scope_id": str(_CUSTOMER_A_SCOPE),
        "room_id": "room-alpha-boardroom",
        "assets": [
            {
                "asset_id": "ast-display-01",
                "room_id": "room-alpha-boardroom",
                "name": "Interactive Display 85",
                "category": "display",
                "manufacturer": "Samsung",
                "model": "QM85R",
                "serial_number": "SN-QM85-9988",
                "status": "operational",
                "installed_at": "2026-08-15T10:00:00Z",
                "warranty_expires_at": "2029-08-15T00:00:00Z",
                "coverage_tier": "gold",
                # Toxic commercial fields:
                "purchase_price": 32000.00,
                "supplier_name": "Distributor X AS",
                "vendor_id": "vend-99",
                "distributor": "AlsoDistributor",
                "margin": 0.22,
                "actual_cost": 24960.00,
            }
        ],
    }

    result = await do_asset_register(mock_engine, params)

    assert result["room_id"] == "room-alpha-boardroom"
    assets = result["assets"]
    assert len(assets) == 1
    asset = assets[0]

    # Required customer fields present
    assert asset["asset_id"] == "ast-display-01"
    assert asset["model"] == "QM85R"
    assert asset["warranty_expires_at"] == "2029-08-15T00:00:00Z"
    assert asset["coverage_tier"] == "gold"

    # Strictly forbidden commercial fields
    for forbidden in (
        "purchase_price",
        "supplier_name",
        "vendor_id",
        "distributor",
        "margin",
        "actual_cost",
    ):
        assert forbidden not in asset, f"Commercial field {forbidden!r} leaked in asset register!"


@pytest.mark.asyncio
async def test_customer_scope_idor_refusal():
    """Attempt by Customer A to query Customer B's room or asset register must be refused."""
    mock_engine = None

    # Trying to query room belonging to customer B while asserting scope A
    with pytest.raises(PermissionError, match="IDOR"):
        await do_room_tracker(
            mock_engine,
            {
                "namespace_id": str(_NAMESPACE_ID),
                "customer_scope_id": str(_CUSTOMER_A_SCOPE),
                "target_scope_id": str(_CUSTOMER_B_SCOPE),  # Mismatched scope -> IDOR
                "room_id": "room-b-secret",
            },
        )

    with pytest.raises(PermissionError, match="IDOR"):
        await do_room_overview(
            mock_engine,
            {
                "namespace_id": str(_NAMESPACE_ID),
                "customer_scope_id": str(_CUSTOMER_A_SCOPE),
                "target_scope_id": str(_CUSTOMER_B_SCOPE),
                "site_id": "site-b-secret",
            },
        )

    with pytest.raises(PermissionError, match="IDOR"):
        await do_asset_register(
            mock_engine,
            {
                "namespace_id": str(_NAMESPACE_ID),
                "customer_scope_id": str(_CUSTOMER_A_SCOPE),
                "target_scope_id": str(_CUSTOMER_B_SCOPE),
                "room_id": "room-b-secret",
            },
        )


def test_portal_app_room_endpoints():
    """Verify HTTP endpoints on dedicated customer portal app."""
    from starlette.testclient import TestClient

    from nce.vertical_modules.customer_portal.app import build_customer_portal_app

    app = build_customer_portal_app()
    client = TestClient(app)

    # 1. Missing scope -> 401 Unauthorized
    resp = client.get("/api/portal/rooms/room-101/tracker")
    assert resp.status_code == 401
    assert "customer scope required" in resp.json()["error"]

    # 2. Authorized tracker request -> 200 OK
    resp = client.get(
        "/api/portal/rooms/room-101/tracker",
        headers={
            "X-Customer-Scope-ID": str(_CUSTOMER_A_SCOPE),
            "X-Namespace-ID": str(_NAMESPACE_ID),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["room_id"] == "room-101"
    assert "stage" in body
    assert "percent_ready" in body

    # 3. Overview request -> 200 OK
    resp = client.get(
        "/api/portal/rooms/overview",
        headers={
            "X-Customer-Scope-ID": str(_CUSTOMER_A_SCOPE),
            "X-Namespace-ID": str(_NAMESPACE_ID),
        },
    )
    assert resp.status_code == 200
    overview = resp.json()
    assert "overall_percent_ready" in overview
    assert "rooms" in overview

    # 4. Asset register request -> 200 OK
    resp = client.get(
        "/api/portal/rooms/room-101/assets",
        headers={
            "X-Customer-Scope-ID": str(_CUSTOMER_A_SCOPE),
            "X-Namespace-ID": str(_NAMESPACE_ID),
        },
    )
    assert resp.status_code == 200
    assets_body = resp.json()
    assert assets_body["room_id"] == "room-101"
    assert "assets" in assets_body
