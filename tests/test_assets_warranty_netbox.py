"""
tests/test_assets_warranty_netbox.py
======================================
Tests for Wave A-3:
  1. Assets Warranty & EOL Watcher (do_check_warranty_eol, handle_assets_check_warranty_eol, api_assets_check_warranty_eol).
  2. NetBox DCIM Bridge (do_sync_netbox, handle_assets_sync_netbox, api_assets_sync_netbox).
  3. Cron Watcher tick (_assets_warranty_eol_tick).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.admin_handlers.assets import (
    api_assets_check_warranty_eol,
    api_assets_sync_netbox,
)
from nce.cron import _assets_warranty_eol_tick
from nce.vertical_modules.assets.mcp_handlers import (
    handle_assets_check_warranty_eol,
    handle_assets_sync_netbox,
)
from nce.vertical_modules.assets.netbox_bridge import _fuzzy_ratio, do_sync_netbox
from nce.vertical_modules.assets.warranty import do_check_warranty_eol

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ASSET_ID_1 = "11111111-1111-4111-8111-111111111111"
_ASSET_ID_2 = "22222222-2222-4222-8222-222222222222"
_ASSET_ID_3 = "33333333-3333-4333-8333-333333333333"


class _async_ctx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_engine(fetch_return=None, fetchrow_return=None) -> tuple[MagicMock, AsyncMock]:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value="UPDATE 1")

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    engine.pg_pool = pool
    return engine, conn


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch):
    class _FakeScoped:
        def __init__(self, pool, ns):
            self._pool = pool
            self._ns = ns

        async def __aenter__(self):
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(
        "nce.vertical_modules.assets.warranty.scoped_pg_session",
        _FakeScoped,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.assets.netbox_bridge.scoped_pg_session",
        _FakeScoped,
    )


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. Warranty & EOL Watcher Core (do_check_warranty_eol)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warranty_eol_empty_assets() -> None:
    engine, conn = _make_engine(fetch_return=[])
    res = await do_check_warranty_eol(engine, {"namespace_id": _NAMESPACE_ID})
    assert res["ok"] is True
    assert res["total_scanned"] == 0
    assert res["warranty_alerts_count"] == 0
    assert res["eol_alerts_count"] == 0
    assert res["warranty_alerts"] == []
    assert res["eol_alerts"] == []


@pytest.mark.asyncio
async def test_warranty_eol_alerts_detection() -> None:
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)

    # Asset 1: Brand new (installed 30 days ago), warranty valid for 23 months -> no alert
    created_1 = now - timedelta(days=30)
    # Asset 2: Installed 720 days ago (24-month warranty expiring in ~10 days) -> warranty alert
    created_2 = now - timedelta(days=720)
    # Asset 3: Installed 1900 days ago (over 5 years / 1825 days) -> lifespan exceeded alert
    created_3 = now - timedelta(days=1900)
    # Asset 4: In EOL lifecycle state
    created_4 = now - timedelta(days=100)

    rows = [
        {
            "id": UUID(_ASSET_ID_1),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "BL-NEW",
            "serial": "SN-NEW",
            "functional_location_id": "ROOM-1",
            "lifecycle_state": "ACTIVE",
            "created_at": created_1,
            "updated_at": created_1,
        },
        {
            "id": UUID(_ASSET_ID_2),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "BL-EXPIRING",
            "serial": "SN-EXP",
            "functional_location_id": "ROOM-2",
            "lifecycle_state": "ACTIVE",
            "created_at": created_2,
            "updated_at": created_2,
        },
        {
            "id": UUID(_ASSET_ID_3),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "BL-OLD",
            "serial": "SN-OLD",
            "functional_location_id": "ROOM-3",
            "lifecycle_state": "ACTIVE",
            "created_at": created_3,
            "updated_at": created_3,
        },
        {
            "id": UUID("44444444-4444-4444-8444-444444444444"),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "BL-EOL",
            "serial": "SN-EOL",
            "functional_location_id": "ROOM-4",
            "lifecycle_state": "EOL",
            "created_at": created_4,
            "updated_at": created_4,
        },
    ]

    engine, conn = _make_engine(fetch_return=rows)
    res = await do_check_warranty_eol(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "now": now,
            "warranty_window_days": 30,
            "eol_window_days": 90,
        },
    )

    assert res["ok"] is True
    assert res["total_scanned"] == 4

    # Warranty alerts: Asset 2 is expiring in ~10 days; Asset 3 expired long ago
    assert res["warranty_alerts_count"] >= 2
    w_ids = [a["asset_id"] for a in res["warranty_alerts"]]
    assert _ASSET_ID_2 in w_ids
    assert _ASSET_ID_3 in w_ids

    # EOL alerts: Asset 3 is lifespan exceeded; Asset 4 is lifecycle_state EOL
    assert res["eol_alerts_count"] >= 2
    e_ids = [a["asset_id"] for a in res["eol_alerts"]]
    assert _ASSET_ID_3 in e_ids
    assert "44444444-4444-4444-8444-444444444444" in e_ids


@pytest.mark.asyncio
async def test_warranty_eol_product_watcher_match(monkeypatch) -> None:
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
    created = now - timedelta(days=60)

    rows = [
        {
            "id": UUID(_ASSET_ID_1),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "Q001:PART-EOL-100",
            "serial": "SN-12345",
            "functional_location_id": "ROOM-AUDITORIUM",
            "lifecycle_state": "ACTIVE",
            "created_at": created,
            "updated_at": created,
        }
    ]

    # Mock product watcher EOL entries
    fake_eol_entries = [
        {
            "mfr_part_no": "PART-EOL-100",
            "manufacturer": "VendorA",
            "successor_mfr_part_no": "PART-NEXT-200",
            "successor_manufacturer": "VendorA",
            "confidence": 0.95,
        }
    ]
    monkeypatch.setattr(
        "nce.vertical_modules.product.watchers._resolve_eol_entries",
        lambda: fake_eol_entries,
    )

    engine, conn = _make_engine(fetch_return=rows)
    res = await do_check_warranty_eol(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "now": now,
        },
    )

    assert res["ok"] is True
    assert res["eol_alerts_count"] == 1
    alert = res["eol_alerts"][0]
    assert alert["asset_id"] == _ASSET_ID_1
    assert alert["is_eol"] is True
    assert alert["successor_mfr_part_no"] == "PART-NEXT-200"


@pytest.mark.asyncio
async def test_warranty_eol_filters() -> None:
    engine, conn = _make_engine(fetch_return=[])
    await do_check_warranty_eol(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "asset_id": _ASSET_ID_1,
        },
    )
    sql_executed = conn.fetch.call_args[0][0]
    assert "AND id = $2::uuid" in sql_executed

    await do_check_warranty_eol(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "functional_location_id": "ROOM-X",
        },
    )
    sql_executed2 = conn.fetch.call_args[0][0]
    assert "AND functional_location_id = $2" in sql_executed2


# ---------------------------------------------------------------------------
# 2. NetBox DCIM Bridge Core (do_sync_netbox)
# ---------------------------------------------------------------------------


def test_fuzzy_ratio_helper() -> None:
    assert _fuzzy_ratio("Cisco Catalyst 9300", "Cisco Catalyst 9300") == 1.0
    assert _fuzzy_ratio("Cisco Catalyst 9300", "cisco catalyst 9300") == 1.0
    assert _fuzzy_ratio("Cisco 9300", "Juniper EX3400") < 0.5
    assert _fuzzy_ratio("", "Device") == 0.0


@pytest.mark.asyncio
async def test_netbox_sync_empty_assets() -> None:
    engine, conn = _make_engine(fetch_return=[])
    res = await do_sync_netbox(engine, {"namespace_id": _NAMESPACE_ID})
    assert res["ok"] is True
    assert res["assets_scanned"] == 0
    assert res["edges_written"] == 0


@pytest.mark.asyncio
async def test_netbox_sync_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("nce.config.cfg.NCE_NETBOX_URL", "")
    monkeypatch.setattr("nce.config.cfg.NCE_NETBOX_TOKEN", "")

    rows = [
        {
            "id": UUID(_ASSET_ID_1),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "BL-1",
            "serial": "SN-001",
            "functional_location_id": "ROOM-1",
            "lifecycle_state": "ACTIVE",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    ]
    engine, conn = _make_engine(fetch_return=rows)
    res = await do_sync_netbox(engine, {"namespace_id": _NAMESPACE_ID})
    assert res["ok"] is True
    assert res["synced"] is False
    assert "not configured" in res["reason"]
    assert res["edges_written"] == 0


@pytest.mark.asyncio
async def test_netbox_sync_match_cascade(monkeypatch) -> None:
    monkeypatch.setattr("nce.config.cfg.NCE_NETBOX_URL", "https://netbox.test")
    monkeypatch.setattr("nce.config.cfg.NCE_NETBOX_TOKEN", "fake-token")

    created = datetime.now(timezone.utc)
    rows = [
        # Match 1: Exact serial match
        {
            "id": UUID(_ASSET_ID_1),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "BL-SWITCH-1",
            "serial": "FCZ123456",
            "functional_location_id": "ROOM-RACK-A",
            "lifecycle_state": "ACTIVE",
            "created_at": created,
            "updated_at": created,
        },
        # Match 2: Custom field match
        {
            "id": UUID(_ASSET_ID_2),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "BL-CF-MATCH",
            "serial": "SN-NO-MATCH",
            "functional_location_id": "ROOM-RACK-B",
            "lifecycle_state": "ACTIVE",
            "created_at": created,
            "updated_at": created,
        },
        # Match 3: Fuzzy name match
        {
            "id": UUID(_ASSET_ID_3),
            "namespace_id": UUID(_NAMESPACE_ID),
            "bom_line_id": "Crestron-DM-NVX-350",
            "serial": None,
            "functional_location_id": "ROOM-CONF-1",
            "lifecycle_state": "ACTIVE",
            "created_at": created,
            "updated_at": created,
        },
    ]

    mock_devices = [
        {
            "id": 101,
            "name": "sw-core-01",
            "serial": "fcz123456",
            "site": {"name": "HQ"},
            "location": {"name": "ROOM-RACK-A"},
            "custom_fields": {},
        },
        {
            "id": 102,
            "name": "ap-floor-02",
            "serial": "OTHER-SERIAL",
            "site": {"name": "HQ"},
            "location": {"name": "ROOM-RACK-B"},
            "custom_fields": {"asset_id": _ASSET_ID_2},
        },
        {
            "id": 103,
            "name": "Crestron-DM-NVX-350-A",
            "serial": "XYZ999",
            "site": {"name": "HQ"},
            "location": {"name": "ROOM-CONF-1"},
            "custom_fields": {},
        },
    ]

    engine, conn = _make_engine(fetch_return=rows)

    with patch(
        "nce.vertical_modules.assets.netbox_bridge.NetBoxAssetClient.fetch_devices",
        new=AsyncMock(return_value=mock_devices),
    ):
        res = await do_sync_netbox(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "fuzzy_threshold": 0.8,
            },
        )

    assert res["ok"] is True
    assert res["synced"] is True
    assert res["assets_scanned"] == 3
    assert res["matched_count"] == 3
    assert res["edges_written"] == 6  # 3 bidirectional matches = 6 edges

    # Verify edge write statements executed on conn
    assert conn.execute.call_count == 6


# ---------------------------------------------------------------------------
# 3. MCP Handlers (handle_assets_check_warranty_eol, handle_assets_sync_netbox)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_assets_check_warranty_eol() -> None:
    engine, conn = _make_engine(fetch_return=[])
    payload_str = await handle_assets_check_warranty_eol(
        engine,
        {"namespace_id": _NAMESPACE_ID},
    )
    data = json.loads(payload_str)
    assert data["ok"] is True
    assert data["namespace_id"] == _NAMESPACE_ID


@pytest.mark.asyncio
async def test_handle_assets_sync_netbox(monkeypatch) -> None:
    monkeypatch.setattr("nce.config.cfg.NCE_NETBOX_URL", "")
    monkeypatch.setattr("nce.config.cfg.NCE_NETBOX_TOKEN", "")

    engine, conn = _make_engine(fetch_return=[])
    payload_str = await handle_assets_sync_netbox(
        engine,
        {"namespace_id": _NAMESPACE_ID},
    )
    data = json.loads(payload_str)
    assert data["ok"] is True
    assert data["namespace_id"] == _NAMESPACE_ID


# ---------------------------------------------------------------------------
# 4. REST Handlers (api_assets_check_warranty_eol, api_assets_sync_netbox)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_assets_check_warranty_eol() -> None:
    from nce.admin_handlers._shared import admin_state

    engine, conn = _make_engine(fetch_return=[])
    admin_state.engine = engine

    req = _make_request(query={"namespace_id": _NAMESPACE_ID, "warranty_window_days": "45"})
    resp = await api_assets_check_warranty_eol(req)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_api_assets_sync_netbox() -> None:
    from nce.admin_handlers._shared import admin_state

    engine, conn = _make_engine(fetch_return=[])
    admin_state.engine = engine

    req = _make_request(body={"namespace_id": _NAMESPACE_ID, "fuzzy_threshold": 0.85})
    resp = await api_assets_sync_netbox(req)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# 5. Cron Watcher Tick (_assets_warranty_eol_tick)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assets_warranty_eol_tick(monkeypatch) -> None:
    pool = AsyncMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"namespace_id": UUID(_NAMESPACE_ID)}])
    pool.acquire = MagicMock(return_value=_async_ctx(conn))

    # Mock unmanaged_pg_connection and acquire_cron_lock
    monkeypatch.setattr(
        "nce.cron.acquire_cron_lock",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "nce.cron.release_cron_lock",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "nce.cron.unmanaged_pg_connection",
        lambda p, site: _async_ctx(conn),
    )

    # Mock do_check_warranty_eol
    mock_check = AsyncMock(
        return_value={
            "ok": True,
            "warranty_alerts_count": 2,
            "eol_alerts_count": 1,
        }
    )
    monkeypatch.setattr(
        "nce.vertical_modules.assets.warranty.do_check_warranty_eol",
        mock_check,
    )

    await _assets_warranty_eol_tick(pool)
    assert mock_check.called
