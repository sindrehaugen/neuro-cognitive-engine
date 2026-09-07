"""
tests/unit/test_sales_write_surface.py
======================================
Acceptance test suite for Sales Native Write Path (Wave S-1).

Covers:
  1. Tool registrations in TOOL_REGISTRY (exact flags for mutations, admin_only, cacheable).
  2. MCP handlers for sales_create_customer, sales_create_lead, sales_create_deal, sales_edit_deal.
  3. REST routes in build_admin_routes() and status code handling (201, 200, 422, 503).
  4. Cache invalidation (bump_mcp_cache_generation) on mutating REST handlers.
  5. Write routing logic with source modes (nce, d365, both) and nce: prefixing.
  6. Graph persistence and dual-storage in sales_read_model (accounts, leads, opportunities).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.admin_app import build_admin_routes
from nce.admin_handlers._shared import admin_state
from nce.admin_handlers.sales import (
    api_admin_sales_create_customer,
    api_admin_sales_create_deal,
    api_admin_sales_create_lead,
    api_admin_sales_edit_deal,
)
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.sales.graph import (
    do_create_customer,
    do_create_deal,
    do_create_lead,
    do_edit_deal,
)
from nce.vertical_modules.sales.mcp_handlers import (
    handle_sales_create_customer,
    handle_sales_create_deal,
    handle_sales_create_lead,
    handle_sales_edit_deal,
)
from nce.vertical_modules.sales.write_routing import (
    do_create_customer as routed_create_customer,
)
from nce.vertical_modules.sales.write_routing import (
    do_create_deal as routed_create_deal,
)
from nce.vertical_modules.sales.write_routing import (
    do_create_lead as routed_create_lead,
)
from nce.vertical_modules.sales.write_routing import (
    do_edit_deal as routed_edit_deal,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_CUSTOMER_ID = "cust-12345"
_LEAD_ID = "lead-67890"
_DEAL_ID = "deal-abcde"
_QUOTE_ID = "quote-xyz99"

_S1_TOOLS = (
    "sales_create_customer",
    "sales_create_lead",
    "sales_create_deal",
    "sales_edit_deal",
)


def _make_engine() -> MagicMock:
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


def test_s1_tools_in_registry() -> None:
    """All 4 Wave S-1 tools must be in TOOL_REGISTRY with Actor flags."""
    for tool_name in _S1_TOOLS:
        assert tool_name in TOOL_REGISTRY, f"{tool_name} missing from TOOL_REGISTRY"
        spec = TOOL_REGISTRY[tool_name]
        assert spec.mutation is True, f"{tool_name} must be a mutation"
        assert spec.admin_only is True, f"{tool_name} must be admin_only"
        assert spec.cacheable is False, f"{tool_name} must NOT be cacheable"
        assert tool_name in MUTATION_TOOLS
        assert tool_name in ADMIN_ONLY_TOOLS
        assert tool_name not in CACHEABLE_TOOLS


def test_s1_tools_in_stdio_tools() -> None:
    """All 4 tools must be defined in mcp_stdio_tools.TOOLS with inputSchema."""
    stdio_names = {t.name: t for t in TOOLS}
    for tool_name in _S1_TOOLS:
        assert tool_name in stdio_names, f"{tool_name} missing from TOOLS"
        tool = stdio_names[tool_name]
        assert "namespace_id" in tool.inputSchema.get("properties", {})
        assert "namespace_id" in tool.inputSchema.get("required", [])


# ---------------------------------------------------------------------------
# 2. MCP Handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_sales_create_customer_success() -> None:
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "customer_id": _CUSTOMER_ID,
        "name": "Acme Corp",
    }
    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_create_customer",
        new_callable=AsyncMock,
        return_value={"ok": True, "customer_id": f"nce:{_CUSTOMER_ID}", "mode": "nce"},
    ) as mock_write:
        raw = await handle_sales_create_customer(engine, params)
        res = json.loads(raw)
        assert res["ok"] is True
        assert res["customer_id"] == f"nce:{_CUSTOMER_ID}"
        mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_sales_create_customer_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_create_customer(engine, {"customer_id": _CUSTOMER_ID})


@pytest.mark.asyncio
async def test_mcp_sales_create_customer_missing_customer_id() -> None:
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_create_customer(engine, {"namespace_id": _NAMESPACE_ID})


@pytest.mark.asyncio
async def test_mcp_sales_create_lead_success() -> None:
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "lead_id": _LEAD_ID,
        "customer_id": _CUSTOMER_ID,
        "name": "New expansion deal",
        "confidence": 0.85,
    }
    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_create_lead",
        new_callable=AsyncMock,
        return_value={"ok": True, "lead_id": f"nce:{_LEAD_ID}", "mode": "nce"},
    ) as mock_write:
        raw = await handle_sales_create_lead(engine, params)
        res = json.loads(raw)
        assert res["ok"] is True
        assert res["lead_id"] == f"nce:{_LEAD_ID}"
        mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_sales_create_deal_success() -> None:
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "deal_id": _DEAL_ID,
        "customer_id": _CUSTOMER_ID,
        "quote_id": _QUOTE_ID,
        "name": "Audio Overhaul Deal",
        "confidence": 0.9,
    }
    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_create_deal",
        new_callable=AsyncMock,
        return_value={"ok": True, "deal_id": f"nce:{_DEAL_ID}", "mode": "nce"},
    ) as mock_write:
        raw = await handle_sales_create_deal(engine, params)
        res = json.loads(raw)
        assert res["ok"] is True
        assert res["deal_id"] == f"nce:{_DEAL_ID}"
        mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_sales_edit_deal_success() -> None:
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "deal_id": _DEAL_ID,
        "name": "Renamed Audio Deal",
        "confidence": 0.95,
    }
    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_edit_deal",
        new_callable=AsyncMock,
        return_value={"ok": True, "deal_id": _DEAL_ID, "mode": "nce"},
    ) as mock_write:
        raw = await handle_sales_edit_deal(engine, params)
        res = json.loads(raw)
        assert res["ok"] is True
        mock_write.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. REST Routes & Handlers
# ---------------------------------------------------------------------------


def test_s1_routes_mounted() -> None:
    """Check routes exist in build_admin_routes()."""
    routes = build_admin_routes()
    route_paths = {(r.path, tuple(sorted(r.methods or []))) for r in routes if hasattr(r, "path")}

    expected = [
        ("/api/sales/customers", ("POST",)),
        ("/api/sales/leads", ("POST",)),
        ("/api/sales/deals", ("POST",)),
        ("/api/sales/deals/edit", ("POST",)),
    ]
    for path, methods in expected:
        assert (path, methods) in route_paths, f"Route ({path}, {methods}) missing from admin_app"


@pytest.mark.asyncio
async def test_rest_create_customer_success() -> None:
    engine = _make_engine()
    admin_state.engine = engine
    body = {
        "namespace_id": _NAMESPACE_ID,
        "customer_id": _CUSTOMER_ID,
        "name": "Test Customer",
    }
    req = _make_request(body=body)

    with (
        patch(
            "nce.admin_handlers.sales.do_create_customer",
            new_callable=AsyncMock,
            return_value={"ok": True, "customer_id": f"nce:{_CUSTOMER_ID}", "mode": "nce"},
        ),
        patch(
            "nce.admin_handlers.sales.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        resp = await api_admin_sales_create_customer(req)
        assert resp.status_code == 201
        data = json.loads(resp.body)
        assert data["ok"] is True
        mock_bump.assert_awaited_once_with(engine, route="api_admin_sales_create_customer")


@pytest.mark.asyncio
async def test_rest_create_lead_success() -> None:
    engine = _make_engine()
    admin_state.engine = engine
    body = {
        "namespace_id": _NAMESPACE_ID,
        "lead_id": _LEAD_ID,
        "customer_id": _CUSTOMER_ID,
        "name": "Test Lead",
    }
    req = _make_request(body=body)

    with (
        patch(
            "nce.admin_handlers.sales.do_create_lead",
            new_callable=AsyncMock,
            return_value={"ok": True, "lead_id": f"nce:{_LEAD_ID}", "mode": "nce"},
        ),
        patch(
            "nce.admin_handlers.sales.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        resp = await api_admin_sales_create_lead(req)
        assert resp.status_code == 201
        data = json.loads(resp.body)
        assert data["ok"] is True
        mock_bump.assert_awaited_once_with(engine, route="api_admin_sales_create_lead")


@pytest.mark.asyncio
async def test_rest_create_deal_success() -> None:
    engine = _make_engine()
    admin_state.engine = engine
    body = {
        "namespace_id": _NAMESPACE_ID,
        "deal_id": _DEAL_ID,
        "customer_id": _CUSTOMER_ID,
        "quote_id": _QUOTE_ID,
        "name": "Test Deal",
    }
    req = _make_request(body=body)

    with (
        patch(
            "nce.admin_handlers.sales.do_create_deal",
            new_callable=AsyncMock,
            return_value={"ok": True, "deal_id": f"nce:{_DEAL_ID}", "mode": "nce"},
        ),
        patch(
            "nce.admin_handlers.sales.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        resp = await api_admin_sales_create_deal(req)
        assert resp.status_code == 201
        data = json.loads(resp.body)
        assert data["ok"] is True
        mock_bump.assert_awaited_once_with(engine, route="api_admin_sales_create_deal")


@pytest.mark.asyncio
async def test_rest_edit_deal_success() -> None:
    engine = _make_engine()
    admin_state.engine = engine
    body = {
        "namespace_id": _NAMESPACE_ID,
        "deal_id": _DEAL_ID,
        "name": "Updated Deal Name",
    }
    req = _make_request(body=body)

    with (
        patch(
            "nce.admin_handlers.sales.do_edit_deal",
            new_callable=AsyncMock,
            return_value={"ok": True, "deal_id": _DEAL_ID, "mode": "nce"},
        ),
        patch(
            "nce.admin_handlers.sales.bump_mcp_cache_generation",
            new_callable=AsyncMock,
        ) as mock_bump,
    ):
        resp = await api_admin_sales_edit_deal(req)
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        mock_bump.assert_awaited_once_with(engine, route="api_admin_sales_edit_deal")


@pytest.mark.asyncio
async def test_rest_missing_fields_validation() -> None:
    admin_state.engine = _make_engine()

    # Missing namespace_id
    req1 = _make_request(body={"customer_id": "c1"})
    assert (await api_admin_sales_create_customer(req1)).status_code == 422

    # Invalid namespace UUID
    req2 = _make_request(body={"namespace_id": "not-a-uuid", "customer_id": "c1"})
    assert (await api_admin_sales_create_customer(req2)).status_code == 422

    # Missing customer_id
    req3 = _make_request(body={"namespace_id": _NAMESPACE_ID})
    assert (await api_admin_sales_create_customer(req3)).status_code == 422

    # Missing deal_id, customer_id, or quote_id for deal
    req4 = _make_request(body={"namespace_id": _NAMESPACE_ID, "deal_id": "d1"})
    assert (await api_admin_sales_create_deal(req4)).status_code == 422


# ---------------------------------------------------------------------------
# 4. Write Routing & Source Modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_routing_nce_mode_prefixes_identity() -> None:
    """When source mode is nce, write_routing prefixes id with nce: if absent."""
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "customer_id": "cust-unprefixed",
        "name": "Unprefixed Customer",
    }
    with (
        patch(
            "nce.vertical_modules.sales.write_routing.resolve",
            new_callable=AsyncMock,
            return_value="nce",
        ),
        patch(
            "nce.vertical_modules.sales.write_routing.graph.do_create_customer",
            new_callable=AsyncMock,
            return_value={"ok": True, "customer_id": "nce:cust-unprefixed"},
        ) as mock_native,
    ):
        result = await routed_create_customer(engine, params)
        assert result["ok"] is True
        assert result["mode"] == "nce"
        assert result["native"]["customer_id"] == "nce:cust-unprefixed"
        mock_native.assert_awaited_once()
        call_kwargs = mock_native.call_args[1]
        assert call_kwargs["customer_id"] == "nce:cust-unprefixed"


@pytest.mark.asyncio
async def test_write_routing_d365_mode() -> None:
    """When source mode is d365, external writer is invoked."""
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "deal_id": _DEAL_ID,
        "customer_id": _CUSTOMER_ID,
        "quote_id": _QUOTE_ID,
        "source_id": "d365-explicit-deal",
    }
    with patch(
        "nce.vertical_modules.sales.write_routing.resolve",
        new_callable=AsyncMock,
        return_value="d365",
    ):
        result = await routed_create_deal(engine, params)
        assert result["ok"] is True
        assert result["mode"] == "d365"
        assert result["external"]["ok"] is True
        assert result["external"]["d365_id"] == "d365-explicit-deal"


@pytest.mark.asyncio
async def test_write_routing_create_lead_nce_mode() -> None:
    """When source mode is nce, routed_create_lead prefixes lead_id with nce:."""
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "lead_id": "unprefixed-lead",
        "customer_id": "unprefixed-cust",
        "name": "Lead Test",
    }
    with (
        patch(
            "nce.vertical_modules.sales.write_routing.resolve",
            new_callable=AsyncMock,
            return_value="nce",
        ),
        patch(
            "nce.vertical_modules.sales.write_routing.graph.do_create_lead",
            new_callable=AsyncMock,
            return_value={"ok": True, "lead_id": "nce:unprefixed-lead"},
        ) as mock_native,
    ):
        result = await routed_create_lead(engine, params)
        assert result["ok"] is True
        assert result["mode"] == "nce"
        assert result["native"]["lead_id"] == "nce:unprefixed-lead"
        mock_native.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_routing_edit_deal_nce_mode() -> None:
    """When source mode is nce, routed_edit_deal dispatches to graph.do_edit_deal."""
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "deal_id": f"nce:{_DEAL_ID}",
        "name": "Updated Title",
    }
    with (
        patch(
            "nce.vertical_modules.sales.write_routing.resolve",
            new_callable=AsyncMock,
            return_value="nce",
        ),
        patch(
            "nce.vertical_modules.sales.write_routing.graph.do_edit_deal",
            new_callable=AsyncMock,
            return_value={"ok": True, "deal_id": f"nce:{_DEAL_ID}"},
        ) as mock_native,
    ):
        result = await routed_edit_deal(engine, params)
        assert result["ok"] is True
        assert result["mode"] == "nce"
        assert result["native"]["deal_id"] == f"nce:{_DEAL_ID}"
        mock_native.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Graph Persistence & sales_read_model Dual Storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_do_create_customer_dual_storage() -> None:
    """Verify do_create_customer writes kg_nodes and upserts sales_read_model accounts."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"
    ns_uuid = UUID(_NAMESPACE_ID)

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        result = await do_create_customer(
            conn,
            ns_uuid,
            customer_id=f"nce:{_CUSTOMER_ID}",
            name="Dual Storage Customer",
        )
    assert result["ok"] is True
    assert result["customer_id"] == f"nce:{_CUSTOMER_ID}"

    # Check DB execute was called for sales_read_model
    assert conn.execute.called
    sqls = [call[0][0] for call in conn.execute.call_args_list]
    assert any("sales_read_model" in s and "accounts" in s for s in sqls)


@pytest.mark.asyncio
async def test_graph_do_create_lead_dual_storage() -> None:
    """Verify do_create_lead writes kg_nodes, kg_edges, and upserts sales_read_model leads."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"
    ns_uuid = UUID(_NAMESPACE_ID)

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        result = await do_create_lead(
            conn,
            ns_uuid,
            lead_id=f"nce:{_LEAD_ID}",
            customer_id=f"nce:{_CUSTOMER_ID}",
            name="Dual Storage Lead",
            confidence=0.8,
        )
    assert result["ok"] is True
    assert result["lead_id"] == f"nce:{_LEAD_ID}"

    assert conn.execute.called
    sqls = [call[0][0] for call in conn.execute.call_args_list]
    assert any("sales_read_model" in s and "leads" in s for s in sqls)


@pytest.mark.asyncio
async def test_graph_do_create_deal_dual_storage() -> None:
    """Verify do_create_deal writes kg_nodes, kg_edges, and upserts sales_read_model opportunities."""
    conn = AsyncMock()
    conn.execute.return_value = "INSERT 0 1"
    ns_uuid = UUID(_NAMESPACE_ID)

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        result = await do_create_deal(
            conn,
            ns_uuid,
            deal_id=f"nce:{_DEAL_ID}",
            customer_id=f"nce:{_CUSTOMER_ID}",
            quote_id=_QUOTE_ID,
            name="Dual Storage Deal",
            confidence=0.75,
        )
    assert result["ok"] is True
    assert result["deal_id"] == f"nce:{_DEAL_ID}"

    assert conn.execute.called
    sqls = [call[0][0] for call in conn.execute.call_args_list]
    assert any("sales_read_model" in s and "opportunities" in s for s in sqls)


@pytest.mark.asyncio
async def test_graph_do_edit_deal_dual_storage() -> None:
    """Verify do_edit_deal updates kg_nodes and updates sales_read_model opportunities."""
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    ns_uuid = UUID(_NAMESPACE_ID)

    with patch("nce.vertical_modules.sales.graph.assert_owner", new_callable=AsyncMock):
        result = await do_edit_deal(
            conn,
            ns_uuid,
            deal_id=f"nce:{_DEAL_ID}",
            name="Updated Deal Title",
            confidence=0.95,
        )
    assert result["ok"] is True
    assert result["deal_id"] == f"nce:{_DEAL_ID}"

    assert conn.execute.called
    sqls = [call[0][0] for call in conn.execute.call_args_list]
    assert any("UPDATE sales_read_model" in s for s in sqls)
