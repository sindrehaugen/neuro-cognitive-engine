"""
tests/unit/test_procurement_bid_resolution_surface.py
=====================================================
Unit tests for Wave PR-4: Procurement BID-price resolution surface and
integration with the shared pricing service (one price truth).

Validates:
  1. ``procurement_resolve_bids`` is registered in ``TOOL_REGISTRY`` with
     correct flags: cacheable=True, admin_only=False, mutation=False.
  2. ``procurement_resolve_bids`` is advertised in ``mcp_stdio_tools.TOOLS``
     with required schema parameters (namespace_id, artnrs).
  3. ``handle_procurement_resolve_bids`` returns valid JSON and respects the
     MCP contract.
  4. ``handle_procurement_resolve_bids`` rejects missing/invalid namespace_id
     and non-list artnrs with McpError(-32602).
  5. ``api_procurement_resolve_bids`` handles GET and POST requests, returning
     HTTP 200 with status="ok".
  6. ``api_procurement_resolve_bids`` returns HTTP 422 on validation failures
     (missing namespace_id, missing artnrs, malformed JSON).
  7. ``api_procurement_resolve_bids`` returns HTTP 503 when the engine is
     disconnected.
  8. ``/api/procurement/bids/resolve`` is mounted in ``build_admin_routes()``.
  9. One price truth integration:
     - ``do_resolve_bids`` annotates rows with source="bid", as_of, and stale.
     - ``resolve_price`` reads the BID cache when product has an artnr/sku
       and customer provides no explicit bid_price.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_app import build_admin_routes
from nce.admin_handlers import procurement as procurement_handlers
from nce.admin_handlers._shared import admin_state
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.pricing.resolver import resolve_price
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.procurement import bids
from nce.vertical_modules.procurement import mcp_handlers as procurement_mcp_handlers

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_NOW = datetime.datetime(2026, 9, 16, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_request(
    body: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    method: str = "POST",
) -> MagicMock:
    """Minimal Starlette-like request mock."""
    req = MagicMock()
    req.method = method
    req.json = AsyncMock(return_value=body if body is not None else {})
    req.query_params = query_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. Tool Registry & Schema Definitions
# ---------------------------------------------------------------------------


def test_procurement_resolve_bids_tool_spec_flags() -> None:
    """procurement_resolve_bids has cacheable=True, admin_only=False, mutation=False."""
    assert "procurement_resolve_bids" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["procurement_resolve_bids"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False


def test_procurement_resolve_bids_in_stdio_tools() -> None:
    """procurement_resolve_bids is defined in TOOLS with valid schema."""
    tool_names = {t.name for t in TOOLS}
    assert "procurement_resolve_bids" in tool_names
    tool = next(t for t in TOOLS if t.name == "procurement_resolve_bids")
    assert tool.inputSchema["type"] == "object"
    assert "namespace_id" in tool.inputSchema["properties"]
    assert "artnrs" in tool.inputSchema["properties"]
    assert set(tool.inputSchema["required"]) == {"namespace_id", "artnrs"}


# ---------------------------------------------------------------------------
# 2. MCP Handler Functional & Error Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_procurement_resolve_bids_success() -> None:
    """Handler dispatches to do_resolve_bids and returns JSON string."""
    engine = MagicMock()
    fake_results = [
        {
            "artnr": "ART-001",
            "leverandor": "SupplierA",
            "bid_id": "BID-1",
            "pris": 99.50,
            "prodid": "PROD-1",
            "source": "bid",
            "as_of": _NOW,
            "stale": False,
        }
    ]
    with patch(
        "nce.vertical_modules.procurement.mcp_handlers.do_resolve_bids",
        new=AsyncMock(return_value={"results": fake_results}),
    ) as mock_core:
        args = {"namespace_id": _NAMESPACE_ID, "artnrs": ["ART-001"]}
        resp_str = await procurement_mcp_handlers.handle_procurement_resolve_bids(engine, args)
        mock_core.assert_awaited_once_with(engine, args)

    parsed = json.loads(resp_str)
    assert "results" in parsed
    assert len(parsed["results"]) == 1
    assert parsed["results"][0]["artnr"] == "ART-001"
    assert parsed["results"][0]["pris"] == 99.50
    assert parsed["results"][0]["source"] == "bid"
    assert parsed["results"][0]["stale"] is False


@pytest.mark.asyncio
async def test_handle_procurement_resolve_bids_missing_namespace() -> None:
    """Missing namespace_id raises McpError with code -32602."""
    engine = MagicMock()
    with pytest.raises(McpError) as exc_info:
        await procurement_mcp_handlers.handle_procurement_resolve_bids(
            engine,
            {"artnrs": ["ART-001"]},
        )
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_procurement_resolve_bids_invalid_artnrs() -> None:
    """Missing or non-list artnrs raises McpError with code -32602."""
    engine = MagicMock()
    with pytest.raises(McpError) as exc_info:
        await procurement_mcp_handlers.handle_procurement_resolve_bids(
            engine,
            {"namespace_id": _NAMESPACE_ID, "artnrs": "ART-001"},
        )
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 3. REST Admin Handler Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_procurement_resolve_bids_post_success() -> None:
    """POST /api/procurement/bids/resolve returns 200 with ok status."""
    fake_results = [{"artnr": "ART-001", "pris": 120.0, "source": "bid", "stale": False}]
    with patch(
        "nce.admin_handlers.procurement.do_resolve_bids",
        new=AsyncMock(return_value={"results": fake_results}),
    ):
        with patch.object(admin_state, "engine", MagicMock()):
            req = _make_request(
                body={"namespace_id": _NAMESPACE_ID, "artnrs": ["ART-001"]},
                method="POST",
            )
            resp = await procurement_handlers.api_procurement_resolve_bids(req)
            assert resp.status_code == 200
            data = json.loads(bytes(resp.body).decode("utf-8"))
            assert data["status"] == "ok"
            assert len(data["results"]) == 1
            assert data["results"][0]["artnr"] == "ART-001"


@pytest.mark.asyncio
async def test_api_procurement_resolve_bids_get_success() -> None:
    """GET /api/procurement/bids/resolve with query params returns 200."""
    fake_results = [{"artnr": "ART-002", "pris": 45.0, "source": "bid", "stale": False}]
    with patch(
        "nce.admin_handlers.procurement.do_resolve_bids",
        new=AsyncMock(return_value={"results": fake_results}),
    ):
        with patch.object(admin_state, "engine", MagicMock()):
            req = _make_request(
                query_params={"namespace_id": _NAMESPACE_ID, "artnrs": "ART-001, ART-002"},
                method="GET",
            )
            resp = await procurement_handlers.api_procurement_resolve_bids(req)
            assert resp.status_code == 200
            data = json.loads(bytes(resp.body).decode("utf-8"))
            assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_api_procurement_resolve_bids_missing_namespace() -> None:
    """Missing namespace_id returns 422."""
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request(body={"artnrs": ["ART-001"]}, method="POST")
        resp = await procurement_handlers.api_procurement_resolve_bids(req)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_procurement_resolve_bids_missing_artnrs() -> None:
    """Missing artnrs returns 422."""
    with patch.object(admin_state, "engine", MagicMock()):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID}, method="POST")
        resp = await procurement_handlers.api_procurement_resolve_bids(req)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_procurement_resolve_bids_invalid_json() -> None:
    """Malformed JSON returns 422."""
    with patch.object(admin_state, "engine", MagicMock()):
        req = MagicMock()
        req.method = "POST"
        req.json = AsyncMock(side_effect=ValueError("bad json"))
        req.query_params = {}
        resp = await procurement_handlers.api_procurement_resolve_bids(req)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_procurement_resolve_bids_engine_disconnected() -> None:
    """Disconnected engine returns 503."""
    with patch.object(admin_state, "engine", None):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID, "artnrs": ["ART-001"]})
        resp = await procurement_handlers.api_procurement_resolve_bids(req)
        assert resp.status_code == 503


def test_procurement_bids_resolve_route_mounted() -> None:
    """Route /api/procurement/bids/resolve is mounted in build_admin_routes()."""
    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/procurement/bids/resolve" in paths


# ---------------------------------------------------------------------------
# 4. One Price Truth Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_best_bids_annotates_freshness_and_source() -> None:
    """_fetch_best_bids annotates rows with source='bid', as_of, and stale flag."""
    conn = AsyncMock()
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    fresh_time = now - datetime.timedelta(minutes=5)
    stale_time = now - datetime.timedelta(days=2)

    fake_db_rows = [
        {
            "artnr": "FRESH-1",
            "leverandor": "SupA",
            "bid_id": "B1",
            "pris": 100.0,
            "prodid": "P1",
            "valid_to": now + datetime.timedelta(days=1),
            "synced_at": fresh_time,
        },
        {
            "artnr": "STALE-1",
            "leverandor": "SupB",
            "bid_id": "B2",
            "pris": 200.0,
            "prodid": "P2",
            "valid_to": now - datetime.timedelta(hours=1),  # expired
            "synced_at": fresh_time,
        },
        {
            "artnr": "STALE-2",
            "leverandor": "SupC",
            "bid_id": "B3",
            "pris": 300.0,
            "prodid": "P3",
            "valid_to": None,
            "synced_at": stale_time,  # age > 86400s
        },
    ]
    conn.fetch = AsyncMock(return_value=fake_db_rows)

    res = await bids._fetch_best_bids(
        conn, uuid.UUID(_NAMESPACE_ID), ["FRESH-1", "STALE-1", "STALE-2"]
    )
    assert len(res) == 3

    r_fresh = next(r for r in res if r["artnr"] == "FRESH-1")
    assert r_fresh["source"] == "bid"
    assert r_fresh["stale"] is False
    assert r_fresh["as_of"] == fresh_time

    r_stale1 = next(r for r in res if r["artnr"] == "STALE-1")
    assert r_stale1["source"] == "bid"
    assert r_stale1["stale"] is True  # expired valid_to

    r_stale2 = next(r for r in res if r["artnr"] == "STALE-2")
    assert r_stale2["source"] == "bid"
    assert r_stale2["stale"] is True  # old synced_at


@pytest.mark.asyncio
async def test_pricing_resolver_unifies_with_procurement_bids() -> None:
    """resolve_price checks procurement_bid_prices when customer has no bid_price."""
    conn = AsyncMock()
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    conn.fetchrow = AsyncMock(return_value={"pris": 85.0, "synced_at": now, "valid_to": None})

    product = {
        "artnr": "SKU-99",
        "supplier_list_price": 100.0,
        "supplier_list_as_of": now,
        "base_price": 120.0,
        "base_as_of": now,
    }
    # Customer provides NO bid_price
    customer = {}

    result = await resolve_price(
        conn,
        namespace_id=_NAMESPACE_ID,
        product=product,
        customer=customer,
    )

    # BID price from procurement cache won over supplier_list and base
    assert result["source"] == "bid"
    assert result["cost"] == 85.0
    assert result["stale"] is False
