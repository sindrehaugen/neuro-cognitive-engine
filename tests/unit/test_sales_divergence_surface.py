"""tests/unit/test_sales_divergence_surface.py
============================================
Unit tests for Wave S-7: Sales Divergence Log Parity Window Reader.

Covers:
  1. Package exports ``do_read_sales_divergence``.
  2. Core ``do_read_sales_divergence`` logic:
     - Clean window (no discrepancies -> flip unblocked).
     - Dirty window (material discrepancies -> flip blocked).
     - Entity filter and window interval sizing.
     - Input validation and pagination.
  3. MCP handler ``handle_sales_divergence_log`` (success + missing arguments).
  4. Admin REST endpoint ``GET /api/sales/divergences`` (503 unconfigured, 422 validation, 200 OK).
  5. Tool registration in ``TOOL_REGISTRY`` and schema in ``mcp_stdio_tools.TOOLS``.
  6. Route mounted in admin app ``build_admin_routes()``.

Wave D-9 (2026-09-20): ``do_read_sales_divergence``'s query logic moved to
the generic ``nce.source_mode.flip.flip_status`` (do_read_sales_divergence
is now a thin ``engine="sales"`` wrapper over it) -- the three tests below
that mock the database connection now patch
``nce.source_mode.flip.scoped_pg_session`` (where the call actually
happens), not ``nce.vertical_modules.sales.flip.scoped_pg_session``. The
assertions and this function's own params/return shape are unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.admin_app import build_admin_routes
from nce.mcp_errors import McpError
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.sales import do_read_sales_divergence
from nce.vertical_modules.sales.mcp_handlers import handle_sales_divergence_log

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
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


def test_sales_exports_do_read_sales_divergence() -> None:
    import nce.vertical_modules.sales as sales

    assert hasattr(sales, "do_read_sales_divergence"), (
        "sales package missing export: do_read_sales_divergence"
    )


@pytest.mark.asyncio
async def test_do_read_sales_divergence_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_read_sales_divergence(engine, {})


@pytest.mark.asyncio
async def test_do_read_sales_divergence_clean_window() -> None:
    engine = _make_engine()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"total_count": 0, "material_count": 0}
    mock_conn.fetch.return_value = []

    with patch(
        "nce.source_mode.flip.scoped_pg_session",
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        ),
    ):
        result = await do_read_sales_divergence(
            engine,
            {"namespace_id": _NAMESPACE_ID, "window_days": 7.0},
        )

    assert result["ok"] is True
    assert result["namespace_id"] == _NAMESPACE_ID
    assert result["engine"] == "sales"
    assert result["window_seconds"] == 7.0 * 86400.0
    assert result["clean"] is True
    assert result["flip_blocked"] is False
    assert result["divergences_count"] == 0
    assert result["material_divergences_count"] == 0
    assert result["items"] == []


@pytest.mark.asyncio
async def test_do_read_sales_divergence_dirty_window() -> None:
    engine = _make_engine()
    now = datetime.now(timezone.utc)
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"total_count": 2, "material_count": 1}
    mock_conn.fetch.return_value = [
        {
            "id": 1,
            "entity": "opportunities",
            "field": "estimated_value",
            "nce_value": "150000",
            "ext_value": "120000",
            "materiality": 0.25,
            "detected_at": now,
        },
        {
            "id": 2,
            "entity": "accounts",
            "field": "name",
            "nce_value": "Acme AS",
            "ext_value": "Acme A/S",
            "materiality": 0.01,
            "detected_at": now,
        },
    ]

    with patch(
        "nce.source_mode.flip.scoped_pg_session",
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        ),
    ):
        result = await do_read_sales_divergence(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "window_seconds": 3600,
                "limit": 50,
                "offset": 10,
            },
        )

    assert result["ok"] is True
    assert result["clean"] is False
    assert result["flip_blocked"] is True
    assert result["divergences_count"] == 2
    assert result["material_divergences_count"] == 1
    assert len(result["items"]) == 2
    assert result["items"][0]["entity"] == "opportunities"
    assert result["items"][0]["is_material"] is True
    assert result["items"][1]["entity"] == "accounts"
    assert result["items"][1]["is_material"] is False


@pytest.mark.asyncio
async def test_do_read_sales_divergence_entity_filter() -> None:
    engine = _make_engine()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"total_count": 1, "material_count": 0}
    mock_conn.fetch.return_value = [
        {
            "id": 10,
            "entity": "accounts",
            "field": "address",
            "nce_value": "Storgata 1",
            "ext_value": "Storgata 2",
            "materiality": 0.02,
            "detected_at": datetime.now(timezone.utc),
        }
    ]

    with patch(
        "nce.source_mode.flip.scoped_pg_session",
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        ),
    ):
        result = await do_read_sales_divergence(
            engine,
            {
                "namespace_id": UUID(_NAMESPACE_ID),
                "entity": "accounts",
            },
        )

    assert result["ok"] is True
    assert result["clean"] is False
    assert result["divergences_count"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["entity"] == "accounts"


@pytest.mark.asyncio
async def test_handle_sales_divergence_log_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(McpError) as exc_info:
        await handle_sales_divergence_log(engine, {})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_sales_divergence_log_success() -> None:
    engine = _make_engine()
    fake_result = {
        "ok": True,
        "namespace_id": _NAMESPACE_ID,
        "engine": "sales",
        "clean": True,
        "flip_blocked": False,
        "divergences_count": 0,
        "material_divergences_count": 0,
        "items": [],
    }
    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_read_sales_divergence",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_core:
        payload = await handle_sales_divergence_log(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "window_days": 14,
                "entity": "opportunities",
                "limit": 20,
                "offset": 5,
            },
        )
        parsed = json.loads(payload)
        assert parsed["ok"] is True
        assert parsed["clean"] is True
        mock_core.assert_awaited_once_with(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "window_days": 14.0,
                "entity": "opportunities",
                "limit": 20,
                "offset": 5,
            },
        )


@pytest.mark.asyncio
async def test_api_admin_sales_divergences_no_engine() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_divergences

    with patch.object(admin_state, "engine", None):
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_admin_sales_divergences(req)
        assert resp.status_code == 503
        data = json.loads(resp.body.decode())
        assert "Engine not connected" in data["error"]


@pytest.mark.asyncio
async def test_api_admin_sales_divergences_missing_or_invalid_namespace() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_divergences

    engine = _make_engine()
    with patch.object(admin_state, "engine", engine):
        req_missing = _make_request(qp={})
        resp_missing = await api_admin_sales_divergences(req_missing)
        assert resp_missing.status_code == 422

        req_invalid = _make_request(qp={"namespace_id": "not-a-uuid"})
        resp_invalid = await api_admin_sales_divergences(req_invalid)
        assert resp_invalid.status_code == 422


@pytest.mark.asyncio
async def test_api_admin_sales_divergences_invalid_query_params() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_divergences

    engine = _make_engine()
    with patch.object(admin_state, "engine", engine):
        req_bad_win = _make_request(qp={"namespace_id": _NAMESPACE_ID, "window_days": "not-a-num"})
        resp_bad_win = await api_admin_sales_divergences(req_bad_win)
        assert resp_bad_win.status_code == 422

        req_bad_lim = _make_request(qp={"namespace_id": _NAMESPACE_ID, "limit": "not-an-int"})
        resp_bad_lim = await api_admin_sales_divergences(req_bad_lim)
        assert resp_bad_lim.status_code == 422


@pytest.mark.asyncio
async def test_api_admin_sales_divergences_success() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_divergences

    engine = _make_engine()
    fake_result = {
        "ok": True,
        "namespace_id": _NAMESPACE_ID,
        "engine": "sales",
        "clean": True,
        "flip_blocked": False,
        "divergences_count": 0,
        "material_divergences_count": 0,
        "items": [],
    }

    with (
        patch.object(admin_state, "engine", engine),
        patch(
            "nce.admin_handlers.sales.do_read_sales_divergence",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_core,
    ):
        req = _make_request(
            qp={
                "namespace_id": _NAMESPACE_ID,
                "window_days": "5",
                "entity": "deals",
                "limit": "25",
                "offset": "0",
            }
        )
        resp = await api_admin_sales_divergences(req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["clean"] is True
        mock_core.assert_awaited_once_with(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "window_days": 5.0,
                "entity": "deals",
                "limit": 25,
                "offset": 0,
            },
        )


def test_tool_registry_sales_divergence_log() -> None:
    assert "sales_divergence_log" in TOOL_REGISTRY, (
        "'sales_divergence_log' not registered in TOOL_REGISTRY"
    )
    spec = TOOL_REGISTRY["sales_divergence_log"]
    assert spec.cacheable is True, "sales_divergence_log must be cacheable"
    assert spec.admin_only is False, (
        "sales_divergence_log must be callable by Advisor (admin_only=False)"
    )
    assert spec.mutation is False, "sales_divergence_log must not be a mutation"
    assert spec.migration is False, "sales_divergence_log must not be a migration"


def test_mcp_stdio_tools_sales_divergence_log() -> None:
    from nce import mcp_stdio_tools

    tool = next((t for t in mcp_stdio_tools.TOOLS if t.name == "sales_divergence_log"), None)
    assert tool is not None, "'sales_divergence_log' missing from mcp_stdio_tools.TOOLS"
    assert "namespace_id" in tool.inputSchema.get("required", [])
    props = tool.inputSchema.get("properties", {})
    assert "window_days" in props
    assert "window_seconds" in props
    assert "entity" in props
    assert "limit" in props
    assert "offset" in props


def test_sales_divergence_route_mounted() -> None:
    routes = build_admin_routes()
    matched = [
        r
        for r in routes
        if getattr(r, "path", None) == "/api/sales/divergences"
        and "GET" in getattr(r, "methods", set())
    ]
    assert len(matched) == 1, "GET /api/sales/divergences route not mounted in admin_app"
