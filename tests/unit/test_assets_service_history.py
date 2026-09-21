"""tests.unit.test_assets_service_history — Unit tests for Wave D-3 Asset Service History.

Tests:
  1. Parameter validation: missing/invalid namespace_id, missing/invalid asset_id.
  2. Asset not found handling (HTTP 404 / not_found=True).
  3. Empty service history (asset with no tickets/work orders/edges).
  4. Composite service history aggregation:
     - Direct support tickets and boundary graph edge tickets.
     - Support ticket actions with intervention types and outcomes.
     - Field tech work orders (ticket source and graph edge links) with outcome scores.
     - Failure pattern graph edges.
  5. Unified chronological timeline ordering and limit constraints.
  6. MCP handler (handle_assets_service_history): parameter validation, JSON output.
  7. Admin REST endpoint (GET /api/assets/{id}/service-history) via Starlette TestClient.
  8. Multi-tenant isolation: strict WHERE namespace_id = $N predicate verification.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nce import admin_state
from nce.admin_handlers import assets as assets_handlers
from nce.vertical_modules.assets.mcp_handlers import handle_assets_service_history
from nce.vertical_modules.assets.service_history import (
    _decode_jsonb,
    do_get_asset_service_history,
)

_NS_ID = UUID("00000000-0000-4000-8000-000000000001")
_ASSET_ID = UUID("11111111-1111-4111-8111-111111111111")
_TICKET_ID_1 = UUID("22222222-2222-4222-8222-222222222222")
_TICKET_ID_2 = UUID("33333333-3333-4333-8333-333333333333")
_WO_ID = "WO-2026-001"


class _async_ctx:
    """Async context manager wrapper for mock db sessions."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


class MockDBConn:
    def __init__(
        self,
        asset_row: Any = None,
        asset_edges: list[Any] | None = None,
        tickets: list[Any] | None = None,
        actions: list[Any] | None = None,
        ticket_edges: list[Any] | None = None,
        work_orders: list[Any] | None = None,
    ) -> None:
        self.asset_row = asset_row
        self.asset_edges = asset_edges or []
        self.tickets = tickets or []
        self.actions = actions or []
        self.ticket_edges = ticket_edges or []
        self.work_orders = work_orders or []
        self.executed_queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.executed_queries.append((query, args))
        if "FROM assets" in query:
            return self.asset_row
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.executed_queries.append((query, args))
        if "FROM kg_edges" in query:
            if "ANY($2::text[])" in query:
                return self.ticket_edges
            return self.asset_edges
        elif "FROM service_tickets" in query:
            return self.tickets
        elif "FROM support_ticket_actions" in query:
            return self.actions
        elif "FROM work_orders" in query:
            return self.work_orders
        return []


def _make_mock_asset(
    asset_id: UUID = _ASSET_ID,
    ns_id: UUID = _NS_ID,
    serial: str = "SN-12345",
    lifecycle_state: str = "IN_SERVICE",
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "namespace_id": ns_id,
        "serial": serial,
        "lifecycle_state": lifecycle_state,
        "is_shell": False,
        "product_id": uuid4(),
        "product_sku": "CORE-SW-24",
        "functional_location_id": "MTR-01",
        "bom_line_id": "BOM-101",
        "created_at": datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_service_history_missing_namespace():
    with pytest.raises(ValueError, match="Missing required parameter: namespace_id"):
        await do_get_asset_service_history(MagicMock(), {"asset_id": str(_ASSET_ID)})


@pytest.mark.asyncio
async def test_service_history_missing_asset_id():
    with pytest.raises(ValueError, match="Missing required parameter: asset_id"):
        await do_get_asset_service_history(MagicMock(), {"namespace_id": str(_NS_ID)})


@pytest.mark.asyncio
async def test_service_history_invalid_uuid():
    with pytest.raises(ValueError, match="Invalid namespace_id UUID"):
        await do_get_asset_service_history(
            MagicMock(),
            {"namespace_id": "not-a-uuid", "asset_id": str(_ASSET_ID)},
        )

    with pytest.raises(ValueError, match="Invalid asset_id UUID"):
        await do_get_asset_service_history(
            MagicMock(),
            {"namespace_id": str(_NS_ID), "asset_id": "bad-uuid"},
        )


@pytest.mark.asyncio
async def test_service_history_asset_not_found():
    conn = MockDBConn(asset_row=None)
    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn),
    ):
        res = await do_get_asset_service_history(
            MagicMock(),
            {"namespace_id": str(_NS_ID), "asset_id": str(_ASSET_ID)},
        )
    assert res["ok"] is False
    assert res["not_found"] is True
    assert "not found" in res["error"]


@pytest.mark.asyncio
async def test_service_history_empty_asset():
    conn = MockDBConn(asset_row=_make_mock_asset())
    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn),
    ):
        res = await do_get_asset_service_history(
            MagicMock(),
            {"namespace_id": str(_NS_ID), "asset_id": str(_ASSET_ID)},
        )
    assert res["ok"] is True
    assert res["asset_id"] == str(_ASSET_ID)
    assert res["asset"]["serial"] == "SN-12345"
    assert res["timeline"] == []
    assert res["tickets"] == []
    assert res["work_orders"] == []
    assert res["actions"] == []
    assert res["summary"]["total_tickets"] == 0
    assert res["summary"]["total_work_orders"] == 0
    assert res["summary"]["total_actions"] == 0


@pytest.mark.asyncio
async def test_service_history_composite_aggregation():
    mock_asset = _make_mock_asset()

    # Graph edge: ticket 2 linked via boundary edge
    asset_edges = [
        {
            "id": uuid4(),
            "subject_label": f"TICKET:{_TICKET_ID_2}",
            "predicate": "about",
            "object_label": f"ASSET:{_ASSET_ID}",
            "confidence": 1.0,
            "change_origin": "agent",
            "created_at": datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc),
        },
        {
            "id": uuid4(),
            "subject_label": f"ASSET:{_ASSET_ID}",
            "predicate": "failure_pattern",
            "object_label": "PRODUCT_SKU:CORE-SW-24",
            "confidence": 0.95,
            "change_origin": "agent",
            "created_at": datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc),
        },
    ]

    # Two tickets (one direct asset_id, one from kg_edges)
    tickets = [
        {
            "id": _TICKET_ID_1,
            "source": "nce",
            "source_id": None,
            "asset_id": _ASSET_ID,
            "room_id": "ROOM-1",
            "customer_id": "CUST-A",
            "status": "resolved",
            "priority": "high",
            "summary": "Switch port intermittent failure",
            "description": "Port 3 drops packets under heavy traffic",
            "sla_profile": "critical_4h",
            "first_response_at": datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc),
            "resolved_at": datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc),
            "ai_diagnosis": {"cause": "bad_cable"},
            "events": [],
            "created_at": datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc),
        },
        {
            "id": _TICKET_ID_2,
            "source": "nce",
            "source_id": None,
            "asset_id": None,  # linked via kg_edges boundary edge
            "room_id": "ROOM-1",
            "customer_id": "CUST-A",
            "status": "open",
            "priority": "medium",
            "summary": "Device fan excessive noise",
            "description": "Audible rattle from chassis fan",
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "ai_diagnosis": {},
            "events": [],
            "created_at": datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc),
        },
    ]

    # Support ticket actions for ticket 1
    actions = [
        {
            "id": uuid4(),
            "ticket_id": _TICKET_ID_1,
            "action_type": "diagnostic",
            "action_summary": "Run port packet loop test",
            "action_details": "1000 packets transmitted, 42 dropped",
            "outcome": "improved",
            "outcome_notes": "Identified connector pin oxidation",
            "performed_by": "tech_lead",
            "performed_at": datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc),
            "created_at": datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc),
        },
        {
            "id": uuid4(),
            "ticket_id": _TICKET_ID_1,
            "action_type": "cable_check",
            "action_summary": "Replaced patch cable",
            "action_details": "Swapped with Cat6A shielded cable",
            "outcome": "resolved",
            "outcome_notes": "0 packet drops over 10m stress test",
            "performed_by": "field_tech_bob",
            "performed_at": datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
            "created_at": datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
        },
    ]

    # Graph edge for work order dispatch
    ticket_edges = [
        {
            "id": uuid4(),
            "subject_label": f"TICKET:{_TICKET_ID_1}",
            "predicate": "dispatched_as",
            "object_label": f"WORK_ORDER:{_WO_ID}",
            "confidence": 1.0,
            "change_origin": "agent",
            "created_at": datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
        }
    ]

    # Work order originating from ticket 1
    work_orders = [
        {
            "id": uuid4(),
            "work_order_id": _WO_ID,
            "namespace_id": _NS_ID,
            "partner_scope_id": None,
            "kind": "service",
            "source_kind": "ticket",
            "source_ref": str(_TICKET_ID_1),
            "location_id": "ROOM-1",
            "assignee_id": "tech_bob",
            "assignee_kind": "employee",
            "status": "completed",
            "priority": "high",
            "summary": "On-site patch cable replacement",
            "due_at": datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc),
            "raw": {
                "outcome": {
                    "rating": 5.0,
                    "quality_score": 1.0,
                    "resolution_notes": "Replaced patch cable and certified line",
                }
            },
            "created_at": datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 7, 2, 15, 45, tzinfo=timezone.utc),
        }
    ]

    conn = MockDBConn(
        asset_row=mock_asset,
        asset_edges=asset_edges,
        tickets=tickets,
        actions=actions,
        ticket_edges=ticket_edges,
        work_orders=work_orders,
    )

    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn),
    ):
        res = await do_get_asset_service_history(
            MagicMock(),
            {"namespace_id": str(_NS_ID), "asset_id": str(_ASSET_ID), "order": "asc"},
        )

    assert res["ok"] is True
    assert res["asset_id"] == str(_ASSET_ID)
    assert len(res["tickets"]) == 2
    assert len(res["actions"]) == 2
    assert len(res["work_orders"]) == 1
    assert len(res["outcome_edges"]) >= 2  # failure_pattern, about, dispatched_as

    # Check summary metrics
    summary = res["summary"]
    assert summary["total_tickets"] == 2
    assert summary["open_tickets"] == 1
    assert summary["resolved_tickets"] == 1
    assert summary["total_actions"] == 2
    assert summary["total_work_orders"] == 1
    assert summary["completed_work_orders"] == 1
    assert summary["action_outcomes"] == {"improved": 1, "resolved": 1}

    # Verify timeline ordering (asc)
    timeline = res["timeline"]
    assert (
        len(timeline) >= 6
    )  # 2 tickets (one resolved), 2 actions, 1 work order, 1 failure pattern
    timestamps = [e["timestamp"] for e in timeline]
    assert timestamps == sorted(timestamps)

    # Verify timeline event types
    event_types = [e["event_type"] for e in timeline]
    assert "ticket_opened" in event_types
    assert "ticket_resolved" in event_types
    assert "ticket_action" in event_types
    assert "work_order" in event_types
    assert "failure_pattern_recorded" in event_types


@pytest.mark.asyncio
async def test_service_history_desc_order_and_limit():
    mock_asset = _make_mock_asset()
    tickets = [
        {
            "id": _TICKET_ID_1,
            "source": "nce",
            "source_id": None,
            "asset_id": _ASSET_ID,
            "room_id": "ROOM-1",
            "customer_id": "CUST-A",
            "status": "open",
            "priority": "low",
            "summary": "Ticket early",
            "description": None,
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "ai_diagnosis": {},
            "events": [],
            "created_at": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        },
        {
            "id": _TICKET_ID_2,
            "source": "nce",
            "source_id": None,
            "asset_id": _ASSET_ID,
            "room_id": "ROOM-1",
            "customer_id": "CUST-A",
            "status": "open",
            "priority": "low",
            "summary": "Ticket late",
            "description": None,
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "ai_diagnosis": {},
            "events": [],
            "created_at": datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc),
        },
    ]

    conn = MockDBConn(asset_row=mock_asset, tickets=tickets)
    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn),
    ):
        res = await do_get_asset_service_history(
            MagicMock(),
            {"namespace_id": str(_NS_ID), "asset_id": str(_ASSET_ID), "order": "desc", "limit": 1},
        )

    assert res["ok"] is True
    assert len(res["timeline"]) == 1
    assert "Ticket late" in res["timeline"][0]["summary"]


@pytest.mark.asyncio
async def test_service_history_multi_tenant_predicates():
    mock_asset = _make_mock_asset()
    conn = MockDBConn(asset_row=mock_asset)
    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn),
    ):
        await do_get_asset_service_history(
            MagicMock(),
            {"namespace_id": str(_NS_ID), "asset_id": str(_ASSET_ID)},
        )

    # Assert EVERY query carried namespace_id
    assert len(conn.executed_queries) >= 3
    for query, args in conn.executed_queries:
        assert "namespace_id = $1::uuid" in query or "namespace_id = $2::uuid" in query
        assert _NS_ID in args


@pytest.mark.asyncio
async def test_handle_assets_service_history_mcp():
    engine = MagicMock()
    conn = MockDBConn(asset_row=_make_mock_asset())
    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn),
    ):
        raw_json = await handle_assets_service_history(
            engine,
            {"namespace_id": str(_NS_ID), "asset_id": str(_ASSET_ID)},
        )
    data = json.loads(raw_json)
    assert data["ok"] is True
    assert data["asset_id"] == str(_ASSET_ID)


def test_api_assets_service_history_rest():
    app = Starlette(
        routes=[
            Route(
                "/api/assets/{id}/service-history",
                endpoint=assets_handlers.api_assets_service_history,
                methods=["GET"],
            )
        ]
    )
    client = TestClient(app)

    # 1. Engine not connected
    admin_state.engine = None
    res = client.get(f"/api/assets/{_ASSET_ID}/service-history?namespace_id={_NS_ID}")
    assert res.status_code == 503

    # 2. Missing namespace_id
    admin_state.engine = MagicMock()
    res = client.get(f"/api/assets/{_ASSET_ID}/service-history")
    assert res.status_code == 422

    # 3. Asset not found
    conn_not_found = MockDBConn(asset_row=None)
    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn_not_found),
    ):
        res = client.get(f"/api/assets/{_ASSET_ID}/service-history?namespace_id={_NS_ID}")
        assert res.status_code == 404
        assert res.json()["not_found"] is True

    # 4. Success 200
    conn_success = MockDBConn(asset_row=_make_mock_asset())
    with patch(
        "nce.vertical_modules.assets.service_history.scoped_pg_session",
        return_value=_async_ctx(conn_success),
    ):
        res = client.get(f"/api/assets/{_ASSET_ID}/service-history?namespace_id={_NS_ID}&limit=10")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["asset_id"] == str(_ASSET_ID)


# ---------------------------------------------------------------------------
# _decode_jsonb -- pure function, no DB. Every mocked test above sets
# ai_diagnosis on the row as an already-parsed dict (see e.g. line ~226),
# never what a real asyncpg connection actually returns (no jsonb codec is
# registered anywhere in this codebase -- the column always arrives as a
# raw JSON string). These tests pin the real decode.
# ---------------------------------------------------------------------------


def test_decode_jsonb_parses_a_real_json_string():
    """The actual regression: `tr["ai_diagnosis"] or {}` never substituted
    for a non-empty string (even `"{}"` is truthy), so the raw string
    passed straight through before this fix."""
    assert _decode_jsonb('{"cause": "bad_cable"}', {}) == {"cause": "bad_cable"}
    assert _decode_jsonb("[]", []) == []


def test_decode_jsonb_falls_back_to_default_on_none():
    assert _decode_jsonb(None, {}) == {}
    assert _decode_jsonb(None, []) == []


def test_decode_jsonb_falls_back_to_default_on_malformed_json():
    assert _decode_jsonb("{not valid json", {}) == {}


def test_decode_jsonb_passes_through_an_already_decoded_value():
    """Not reachable against a real connection today (no jsonb codec is
    registered), but a dict/list must still pass through unchanged if one
    is ever handed in directly."""
    assert _decode_jsonb({"cause": "bad_cable"}, {}) == {"cause": "bad_cable"}
    assert _decode_jsonb([1, 2], []) == [1, 2]
