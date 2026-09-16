"""tests/unit/test_sales_stalled_and_brief.py
============================================
Unit tests for Wave S-4: Sales Stalled-Deal Watcher and Morning Brief Slice.

Covers:
  1. Core ``do_stalled_deal_watcher``:
     - Missing namespace validation.
     - Clean state (no stalled deals).
     - Detection of stalled deals (statecode 0 and old updated_at).
     - Skipping closed/won/lost deals (statecode != 0).
     - Custom slip_days parameter.
  2. Core ``do_morning_brief_slice``:
     - Missing namespace validation.
     - Pipeline calculation (open deals, estimatedvalue).
     - At-risk deals count (> 14 days inactive).
     - Won deals aggregation (within period_days lookback).
     - Handling of naive vs aware timestamps and json payload types.
  3. Cron runner ``_sales_stalled_deal_watcher_tick``:
     - Lock skipping when held.
     - Lock acquisition, namespace scanning, alert dispatch on detection.
     - Per-namespace and global error handling and lock release.
  4. MCP tool handler ``handle_sales_morning_brief_slice``:
     - Missing namespace error.
     - Successful dispatch and JSON formatting.
  5. Admin REST handler ``api_admin_sales_morning_brief_slice``:
     - 503 when engine not connected.
     - 422 validation errors (missing/bad namespace, non-integer period).
     - 200 OK success response.
     - Error handling (ValueError -> 422, Exception -> 500).
  6. Surface invariants:
     - Tool registration in ``TOOL_REGISTRY`` (cacheable, not admin_only, not mutation).
     - Tool schema in ``mcp_stdio_tools.TOOLS``.
     - Route mounted in ``build_admin_routes()``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.admin_app import build_admin_routes
from nce.mcp_errors import McpError
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.sales import (
    do_morning_brief_slice,
    do_stalled_deal_watcher,
)
from nce.vertical_modules.sales.mcp_handlers import handle_sales_morning_brief_slice

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


# ---------------------------------------------------------------------------
# 1. do_stalled_deal_watcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_stalled_deal_watcher_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_stalled_deal_watcher(engine, {})


@pytest.mark.asyncio
async def test_do_stalled_deal_watcher_clean() -> None:
    engine = _make_engine()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    with patch(
        "nce.vertical_modules.sales.flip.scoped_pg_session",
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        ),
    ):
        result = await do_stalled_deal_watcher(
            engine,
            {"namespace_id": _NAMESPACE_ID, "slip_days": 30},
        )

    assert result["ok"] is True
    assert result["stalled_deals_count"] == 0
    assert result["stalled_deals"] == []


@pytest.mark.asyncio
async def test_do_stalled_deal_watcher_detected_and_filtered() -> None:
    engine = _make_engine()
    old_time = datetime.now(timezone.utc) - timedelta(days=40)

    rows = [
        # Stalled open deal (statecode 0)
        {
            "source_id": "deal-1",
            "name": "Acme Corp Expansion",
            "updated_at": old_time,
            "source_json": json.dumps({"statecode": 0, "estimatedvalue": 50000}),
        },
        # Stalled open deal with dict payload and no explicit statecode (defaults to open)
        {
            "source_id": "deal-2",
            "name": "Beta Labs Project",
            "updated_at": old_time,
            "source_json": {"estimatedvalue": 25000},
        },
        # Won deal (statecode 1) -> must be skipped
        {
            "source_id": "deal-won",
            "name": "Gamma Corp Won",
            "updated_at": old_time,
            "source_json": {"statecode": 1, "actualvalue": 100000},
        },
        # Lost deal (statecode 2) -> must be skipped
        {
            "source_id": "deal-lost",
            "name": "Delta Corp Lost",
            "updated_at": old_time,
            "source_json": {"statecode": "2", "estimatedvalue": 10000},
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = rows

    with patch(
        "nce.vertical_modules.sales.flip.scoped_pg_session",
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        ),
    ):
        result = await do_stalled_deal_watcher(
            engine,
            {"namespace_id": _NAMESPACE_ID, "slip_days": 30},
        )

    assert result["ok"] is True
    assert result["stalled_deals_count"] == 2
    deal_ids = [d["deal_id"] for d in result["stalled_deals"]]
    assert "deal-1" in deal_ids
    assert "deal-2" in deal_ids
    assert "deal-won" not in deal_ids
    assert "deal-lost" not in deal_ids


# ---------------------------------------------------------------------------
# 2. do_morning_brief_slice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_morning_brief_slice_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_morning_brief_slice(engine, {})


@pytest.mark.asyncio
async def test_do_morning_brief_slice_metrics() -> None:
    engine = _make_engine()
    now = datetime.now(timezone.utc)
    fresh_time = now - timedelta(days=2)
    stale_time = now - timedelta(days=20)
    old_won_time = now - timedelta(days=15)

    rows = [
        # Open deal, active (<14 days old)
        {
            "source_id": "d-active",
            "name": "Active Deal",
            "updated_at": fresh_time,
            "source_json": {"statecode": 0, "estimatedvalue": 10000.0},
        },
        # Open deal, at risk (>14 days old, naive datetime)
        {
            "source_id": "d-stale",
            "name": "Stale Deal",
            "updated_at": stale_time.replace(tzinfo=None),
            "source_json": json.dumps({"statecode": "0", "estimatedvalue": 25000.0}),
        },
        # Won deal within period (period_days=7)
        {
            "source_id": "d-won-recent",
            "name": "Won Recent",
            "updated_at": fresh_time,
            "source_json": {"statecode": 1, "actualvalue": 45000.0},
        },
        # Won deal within period without actualvalue (falls back to estimatedvalue)
        {
            "source_id": "d-won-est",
            "name": "Won Fallback",
            "updated_at": fresh_time,
            "source_json": {"statecode": 1, "estimatedvalue": 15000.0},
        },
        # Won deal outside period (> 7 days)
        {
            "source_id": "d-won-old",
            "name": "Won Old",
            "updated_at": old_won_time,
            "source_json": {"statecode": 1, "actualvalue": 80000.0},
        },
        # Lost deal
        {
            "source_id": "d-lost",
            "name": "Lost Deal",
            "updated_at": fresh_time,
            "source_json": {"statecode": 2, "estimatedvalue": 5000.0},
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = rows

    with patch(
        "nce.vertical_modules.sales.flip.scoped_pg_session",
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        ),
    ):
        result = await do_morning_brief_slice(
            engine,
            {"namespace_id": _NAMESPACE_ID, "period_days": 7},
        )

    assert result["ok"] is True
    # pipeline_value = 10000 + 25000 = 35000.0
    assert result["pipeline_value"] == 35000.0
    # at_risk_deals_count = 1 (d-stale)
    assert result["at_risk_deals_count"] == 1
    # won_value_this_period = 45000 + 15000 = 60000.0
    assert result["won_value_this_period"] == 60000.0
    assert result["won_count_this_period"] == 2


# ---------------------------------------------------------------------------
# 3. Cron tick runner (_sales_stalled_deal_watcher_tick)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_sales_stalled_deal_watcher_tick_runs_with_lock() -> None:
    from nce.cron import _sales_stalled_deal_watcher_tick

    ns_id = uuid4()
    pool = MagicMock()
    mock_lock = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"id": ns_id}]

    mock_release = AsyncMock()
    mock_acquire = AsyncMock(return_value=mock_lock)

    fake_unmanaged_ctx = AsyncMock()
    fake_unmanaged_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    fake_unmanaged_ctx.__aexit__ = AsyncMock(return_value=None)

    fake_stats = {
        "ok": True,
        "stalled_deals_count": 2,
        "stalled_deals": [
            {"deal_id": "d-1", "name": "Alpha Corp"},
            {"deal_id": "d-2", "name": "Omega Inc"},
        ],
    }

    with (
        patch("nce.cron.acquire_cron_lock", mock_acquire),
        patch("nce.cron.release_cron_lock", mock_release),
        patch("nce.cron.unmanaged_pg_connection", return_value=fake_unmanaged_ctx),
        patch(
            "nce.vertical_modules.sales.flip.do_stalled_deal_watcher",
            new_callable=AsyncMock,
            return_value=fake_stats,
        ) as mock_watch,
        patch("nce.cron._dispatch_throttled_alert", new_callable=AsyncMock) as mock_alert,
    ):
        await _sales_stalled_deal_watcher_tick(pool)

        mock_acquire.assert_awaited_once_with("sales_stalled_deal_watcher", 3660)
        mock_watch.assert_awaited_once()
        mock_alert.assert_awaited_once()
        alert_args = mock_alert.await_args[0]
        assert f"cron.sales_stalled_deal_watcher.{ns_id}" == alert_args[0]
        assert "Stalled Deals Detected" in alert_args[1]
        mock_release.assert_awaited_once_with(mock_lock)


@pytest.mark.asyncio
async def test_cron_sales_stalled_deal_watcher_tick_skips_when_lock_held() -> None:
    from nce.cron import _sales_stalled_deal_watcher_tick

    pool = MagicMock()
    mock_acquire = AsyncMock(return_value=None)
    mock_release = AsyncMock()

    with (
        patch("nce.cron.acquire_cron_lock", mock_acquire),
        patch("nce.cron.release_cron_lock", mock_release),
        patch("nce.cron.unmanaged_pg_connection") as mock_unmanaged,
    ):
        await _sales_stalled_deal_watcher_tick(pool)
        mock_unmanaged.assert_not_called()
        mock_release.assert_not_called()


@pytest.mark.asyncio
async def test_cron_sales_stalled_deal_watcher_tick_handles_namespace_error() -> None:
    from nce.cron import _sales_stalled_deal_watcher_tick

    ns_id = uuid4()
    pool = MagicMock()
    mock_lock = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"id": ns_id}]

    mock_release = AsyncMock()
    mock_acquire = AsyncMock(return_value=mock_lock)

    fake_unmanaged_ctx = AsyncMock()
    fake_unmanaged_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    fake_unmanaged_ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("nce.cron.acquire_cron_lock", mock_acquire),
        patch("nce.cron.release_cron_lock", mock_release),
        patch("nce.cron.unmanaged_pg_connection", return_value=fake_unmanaged_ctx),
        patch(
            "nce.vertical_modules.sales.flip.do_stalled_deal_watcher",
            new_callable=AsyncMock,
            side_effect=TimeoutError("DB query timeout"),
        ),
        patch("nce.cron._dispatch_throttled_alert", new_callable=AsyncMock) as mock_alert,
    ):
        await _sales_stalled_deal_watcher_tick(pool)

        mock_alert.assert_awaited_once()
        alert_args = mock_alert.await_args[0]
        assert "Sales Stalled Deal Watcher Failed" in alert_args[1]
        mock_release.assert_awaited_once_with(mock_lock)


# ---------------------------------------------------------------------------
# 4. MCP Handler (handle_sales_morning_brief_slice)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sales_morning_brief_slice_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_morning_brief_slice(engine, {})


@pytest.mark.asyncio
async def test_handle_sales_morning_brief_slice_success() -> None:
    engine = _make_engine()
    fake_result = {
        "ok": True,
        "pipeline_value": 42000.0,
        "at_risk_deals_count": 2,
        "won_value_this_period": 10000.0,
        "won_count_this_period": 1,
    }

    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_morning_brief_slice",
        new_callable=AsyncMock,
        return_value=fake_result,
    ) as mock_core:
        raw_res = await handle_sales_morning_brief_slice(
            engine,
            {"namespace_id": _NAMESPACE_ID, "period_days": 14},
        )
        data = json.loads(raw_res)
        assert data["ok"] is True
        assert data["pipeline_value"] == 42000.0
        mock_core.assert_awaited_once_with(
            engine,
            {"namespace_id": _NAMESPACE_ID, "period_days": 14},
        )


# ---------------------------------------------------------------------------
# 5. Admin REST Handler (api_admin_sales_morning_brief_slice)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_admin_sales_morning_brief_slice_no_engine() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_morning_brief_slice

    with patch.object(admin_state, "engine", None):
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_admin_sales_morning_brief_slice(req)
        assert resp.status_code == 503
        data = json.loads(resp.body.decode())
        assert "Engine not connected" in data["error"]


@pytest.mark.asyncio
async def test_api_admin_sales_morning_brief_slice_validations() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_morning_brief_slice

    engine = _make_engine()
    with patch.object(admin_state, "engine", engine):
        # Missing namespace
        req1 = _make_request(qp={})
        resp1 = await api_admin_sales_morning_brief_slice(req1)
        assert resp1.status_code == 422

        # Invalid UUID
        req2 = _make_request(qp={"namespace_id": "not-valid-uuid"})
        resp2 = await api_admin_sales_morning_brief_slice(req2)
        assert resp2.status_code == 422

        # Invalid period_days
        req3 = _make_request(qp={"namespace_id": _NAMESPACE_ID, "period_days": "not-int"})
        resp3 = await api_admin_sales_morning_brief_slice(req3)
        assert resp3.status_code == 422
        data3 = json.loads(resp3.body.decode())
        assert "period_days must be an integer" in data3["error"]


@pytest.mark.asyncio
async def test_api_admin_sales_morning_brief_slice_success() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_morning_brief_slice

    engine = _make_engine()
    fake_result = {
        "ok": True,
        "pipeline_value": 75000.0,
        "at_risk_deals_count": 0,
        "won_value_this_period": 25000.0,
        "won_count_this_period": 2,
    }

    with (
        patch.object(admin_state, "engine", engine),
        patch(
            "nce.admin_handlers.sales.do_morning_brief_slice",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_core,
    ):
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID, "period_days": "30"})
        resp = await api_admin_sales_morning_brief_slice(req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["pipeline_value"] == 75000.0
        mock_core.assert_awaited_once_with(
            engine,
            {"namespace_id": _NAMESPACE_ID, "period_days": 30},
        )


@pytest.mark.asyncio
async def test_api_admin_sales_morning_brief_slice_exceptions() -> None:
    from nce import admin_state
    from nce.admin_handlers.sales import api_admin_sales_morning_brief_slice

    engine = _make_engine()
    with (
        patch.object(admin_state, "engine", engine),
        patch(
            "nce.admin_handlers.sales.do_morning_brief_slice",
            new_callable=AsyncMock,
            side_effect=ValueError("Invalid request parameters"),
        ),
    ):
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_admin_sales_morning_brief_slice(req)
        assert resp.status_code == 422

    with (
        patch.object(admin_state, "engine", engine),
        patch(
            "nce.admin_handlers.sales.do_morning_brief_slice",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Database failure"),
        ),
    ):
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID})
        resp = await api_admin_sales_morning_brief_slice(req)
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# 6. Surface Invariants & Routes
# ---------------------------------------------------------------------------


def test_tool_registry_sales_morning_brief_slice() -> None:
    assert "sales_morning_brief_slice" in TOOL_REGISTRY, (
        "'sales_morning_brief_slice' not registered in TOOL_REGISTRY"
    )
    spec = TOOL_REGISTRY["sales_morning_brief_slice"]
    assert spec.cacheable is True, "sales_morning_brief_slice must be cacheable"
    assert spec.admin_only is False, (
        "sales_morning_brief_slice must be callable by Advisor (admin_only=False)"
    )
    assert spec.mutation is False, "sales_morning_brief_slice must not be a mutation"
    assert spec.migration is False, "sales_morning_brief_slice must not be a migration"


def test_mcp_stdio_tools_sales_morning_brief_slice() -> None:
    from nce import mcp_stdio_tools

    tool = next((t for t in mcp_stdio_tools.TOOLS if t.name == "sales_morning_brief_slice"), None)
    assert tool is not None, "'sales_morning_brief_slice' missing from mcp_stdio_tools.TOOLS"
    assert "namespace_id" in tool.inputSchema.get("required", [])
    props = tool.inputSchema.get("properties", {})
    assert "period_days" in props


def test_sales_morning_brief_route_mounted() -> None:
    routes = build_admin_routes()
    matched = [
        r
        for r in routes
        if getattr(r, "path", None) == "/api/sales/morning-brief"
        and "GET" in getattr(r, "methods", set())
    ]
    assert len(matched) == 1, "GET /api/sales/morning-brief route not mounted in admin_app"
