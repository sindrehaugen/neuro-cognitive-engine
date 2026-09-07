"""
tests/unit/test_sales_commission_surface.py
===========================================
Acceptance test suite for Sales Commission Calculation Surface Completion (Wave S-6).

Covers:
  1. Tool registration in TOOL_REGISTRY (cacheable=True, mutation=False, admin_only=False).
  2. Stdio tool definition in mcp_stdio_tools.TOOLS with inputSchema.
  3. MCP handler handle_sales_calculate_commission with direct deal_data and historical event queries.
  4. Input validation (missing namespace_id, malformed payloads).
  5. REST route in build_admin_routes() and api_admin_sales_calculate_commission handler status codes (200, 422, 503).
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_app import build_admin_routes
from nce.admin_handlers._shared import admin_state
from nce.admin_handlers.sales import api_admin_sales_calculate_commission
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.sales.mcp_handlers import handle_sales_calculate_commission

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_SELLER_ID = "seller-42"


def _make_engine() -> MagicMock:
    """Mock NCEEngine with asyncpg pool supporting scoped_pg_session."""
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = {"owner_engine": "sales"}
    conn.fetch.return_value = []
    conn.execute.return_value = "INSERT 0 1"

    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = None
    conn.transaction = MagicMock(return_value=tx)

    ctx = AsyncMock()
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = None

    pool = MagicMock()
    pool.acquire.return_value = ctx
    engine.pg_pool = pool
    engine.pool = pool
    return engine


def _make_request(
    qp: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.query_params = qp or {}
    req.path_params = path_params or {}
    if body is not None:
        req.json = AsyncMock(return_value=body)
    else:
        req.json = AsyncMock(side_effect=Exception("No JSON body"))
    return req


# ---------------------------------------------------------------------------
# 1. TOOL_REGISTRY & stdio definitions
# ---------------------------------------------------------------------------


def test_sales_calculate_commission_in_registry() -> None:
    """sales_calculate_commission must be in TOOL_REGISTRY with Advisor read flags."""
    assert "sales_calculate_commission" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["sales_calculate_commission"]
    assert spec.cacheable is True, "sales_calculate_commission must be cacheable"
    assert spec.mutation is False, "sales_calculate_commission must NOT be a mutation"
    assert spec.admin_only is False, "sales_calculate_commission must NOT be admin_only"
    assert spec.migration is False, "sales_calculate_commission must NOT be migration"
    assert "sales_calculate_commission" in CACHEABLE_TOOLS
    assert "sales_calculate_commission" not in MUTATION_TOOLS
    assert "sales_calculate_commission" not in ADMIN_ONLY_TOOLS


def test_sales_calculate_commission_in_stdio_tools() -> None:
    """sales_calculate_commission must be in TOOLS with valid inputSchema."""
    stdio_names = {t.name: t for t in TOOLS}
    assert "sales_calculate_commission" in stdio_names
    tool = stdio_names["sales_calculate_commission"]
    props = tool.inputSchema.get("properties", {})
    assert "namespace_id" in props
    assert "seller_id" in props
    assert "deal_data" in props
    assert "namespace_id" in tool.inputSchema.get("required", [])


# ---------------------------------------------------------------------------
# 2. MCP Handler Execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_calculate_commission_deal_data() -> None:
    """Direct deal_data calculation returns accurate margin-weighted commission."""
    engine = _make_engine()
    deal_data = {
        "items": [
            {"type": "hardware", "price": 1000.0, "cost": 500.0},
            {"type": "service", "price": 500.0, "cost": 200.0},
        ]
    }
    raw = await handle_sales_calculate_commission(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "deal_data": deal_data,
        },
    )
    result = json.loads(raw)
    assert result["ok"] is True
    assert "commission" in result
    assert result["commission"] > 0.0
    assert "config_version" in result


@pytest.mark.asyncio
async def test_mcp_calculate_commission_missing_namespace() -> None:
    """Missing namespace_id must raise McpError."""
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_calculate_commission(engine, {})


@pytest.mark.asyncio
async def test_mcp_calculate_commission_ledger_events() -> None:
    """Historical deal_won query against event_log aggregates commission for seller."""
    engine = _make_engine()

    mock_row_1 = {
        "id": "11111111-1111-4111-8111-111111111111",
        "event_seq": 10,
        "occurred_at": datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=datetime.timezone.utc),
        "params": json.dumps(
            {
                "decision_type": "deal_won",
                "details": {
                    "seller_id": _SELLER_ID,
                    "quote_id": "quote-1",
                    "items": [
                        {"type": "hardware", "price": 2000.0, "cost": 1000.0},
                    ],
                },
            }
        ),
    }
    mock_row_other_seller = {
        "id": "22222222-2222-4222-8222-222222222222",
        "event_seq": 11,
        "occurred_at": datetime.datetime(2026, 9, 2, 12, 0, 0, tzinfo=datetime.timezone.utc),
        "params": {
            "decision_type": "deal_won",
            "details": {
                "seller_id": "other-seller",
                "quote_id": "quote-2",
                "items": [
                    {"type": "service", "price": 5000.0, "cost": 1000.0},
                ],
            },
        },
    }

    ctx = engine.pg_pool.acquire.return_value
    conn = ctx.__aenter__.return_value
    conn.fetch.return_value = [mock_row_1, mock_row_other_seller]

    raw = await handle_sales_calculate_commission(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "seller_id": _SELLER_ID,
        },
    )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["seller_id"] == _SELLER_ID
    assert len(result["commissions"]) == 1
    assert result["commissions"][0]["seller_id"] == _SELLER_ID
    assert result["commissions"][0]["quote_id"] == "quote-1"
    assert result["total_commission"] > 0.0


# ---------------------------------------------------------------------------
# 3. REST Endpoint
# ---------------------------------------------------------------------------


def test_rest_route_mounted() -> None:
    """GET /api/sales/commission must be mounted in build_admin_routes()."""
    routes = build_admin_routes()
    matching = [
        r
        for r in routes
        if getattr(r, "path", None) == "/api/sales/commission"
        and "GET" in getattr(r, "methods", set())
    ]
    assert len(matching) == 1, "GET /api/sales/commission must be mounted exactly once"
    assert matching[0].endpoint == api_admin_sales_calculate_commission


@pytest.mark.asyncio
async def test_rest_calculate_commission_success() -> None:
    """Valid GET /api/sales/commission returns 200 with result."""
    engine = _make_engine()
    admin_state.engine = engine
    req = _make_request(qp={"namespace_id": _NAMESPACE_ID, "seller_id": _SELLER_ID})

    mock_result = {
        "ok": True,
        "seller_id": _SELLER_ID,
        "total_commission": 450.0,
        "commissions": [],
        "config_version": "2026-Q3",
    }
    with patch(
        "nce.admin_handlers.sales.do_calculate_commission",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await api_admin_sales_calculate_commission(req)
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        assert data["seller_id"] == _SELLER_ID
        assert data["total_commission"] == 450.0


@pytest.mark.asyncio
async def test_rest_calculate_commission_missing_namespace() -> None:
    """Missing namespace_id query param returns 422."""
    engine = _make_engine()
    admin_state.engine = engine
    req = _make_request(qp={})
    resp = await api_admin_sales_calculate_commission(req)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rest_calculate_commission_invalid_namespace() -> None:
    """Invalid UUID namespace_id query param returns 422."""
    engine = _make_engine()
    admin_state.engine = engine
    req = _make_request(qp={"namespace_id": "not-a-uuid"})
    resp = await api_admin_sales_calculate_commission(req)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rest_calculate_commission_no_engine() -> None:
    """Disconnected engine returns 503."""
    admin_state.engine = None
    req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
    resp = await api_admin_sales_calculate_commission(req)
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_rest_calculate_commission_error_handling() -> None:
    """Internal exceptions return 500 admin error response."""
    engine = _make_engine()
    admin_state.engine = engine
    req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
    with patch(
        "nce.admin_handlers.sales.do_calculate_commission",
        new_callable=AsyncMock,
        side_effect=RuntimeError("database offline"),
    ):
        resp = await api_admin_sales_calculate_commission(req)
        assert resp.status_code == 500
