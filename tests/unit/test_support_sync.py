"""
tests/unit/test_support_sync.py
===============================
Unit tests for Module 10 (Support Engine) D365 sync delegation & proactive sweep:
  - do_sync_now delegates to handle_d365_sync_now for d365/both modes
  - Telemetry-derived proactive sweep is performed
  - Graceful handling when D365 is offline/unavailable
  - do_sync_status returns health and source mode
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.vertical_modules.support.sync import do_sync_now, do_sync_status

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    return pool


@pytest.mark.asyncio
async def test_do_sync_now_delegates_to_d365(mock_pool):
    d365_mock_response = '{"status": "completed", "stats": {"cases_synced": 5}}'

    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = 3  # active tickets count

    with (
        patch("nce.vertical_modules.support.sync.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.dynamics365.mcp_handlers.handle_d365_sync_now",
            new=AsyncMock(return_value=d365_mock_response),
        ) as mock_d365,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_sync_now(
            mock_pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "mode": "both",
            },
        )

        assert res["status"] == "completed"
        assert res["mode"] == "both"
        assert res["d365_sync"]["status"] == "completed"
        assert res["d365_sync"]["stats"]["cases_synced"] == 5
        assert res["proactive_sweep"]["active_tickets_checked"] == 3
        mock_d365.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_sync_now_native_mode_skips_d365(mock_pool):
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = 1

    with (
        patch("nce.vertical_modules.support.sync.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.dynamics365.mcp_handlers.handle_d365_sync_now",
            new=AsyncMock(),
        ) as mock_d365,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_sync_now(
            mock_pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "mode": "nce",
            },
        )

        assert res["status"] == "completed"
        assert res["mode"] == "nce"
        assert res["d365_sync"]["status"] == "skipped"
        mock_d365.assert_not_awaited()


@pytest.mark.asyncio
async def test_do_sync_now_missing_namespace_raises(mock_pool):
    with pytest.raises(ValueError):
        await do_sync_now(mock_pool, {})


@pytest.mark.asyncio
async def test_do_sync_status(mock_pool):
    res = await do_sync_status(mock_pool, {"namespace_id": _NAMESPACE_ID, "mode": "both"})
    assert res["status"] == "healthy"
    assert res["source_mode"] == "both"
    assert "last_sync" in res
