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
from datetime import date, datetime, timedelta, timezone
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
from nce.vertical_modules.assets.warranty import (
    do_check_warranty_eol,
    evaluate_regulatory_compliance,
)
from nce.vertical_modules.system_design.dnv_marine import (
    validate_atex_and_environment,
    validate_dnv_location_classes,
    validate_dnv_switchboard_and_bus,
    validate_marine_cable_iec60092,
)

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


# ---------------------------------------------------------------------------
# 6. Maritime & Offshore Compliance Tests (Wave E-2)
# ---------------------------------------------------------------------------


def test_dnv_switchboard_valid_installation() -> None:
    """Test fully compliant marine switchboard, IT earthing, dual feed consumers, and THD."""
    consumers = [
        {
            "name": "Steering Gear Pump 1",
            "service_type": "steering",
            "feed_count": 2,
            "has_emergency_feed": True,
            "ups_autonomy_minutes": 45,  # >= 30 min required
        },
        {
            "name": "Emergency Lighting Deck 3",
            "service_type": "emergency_lighting",
            "feed_count": 2,
            "has_emergency_feed": True,
            "ups_autonomy_hours": 20,  # >= 18 h required
        },
        {
            "name": "Navigation Radar & ECDIS",
            "service_type": "navigation",
            "feed_count": 2,
            "has_emergency_feed": True,
            "ups_autonomy_seconds": 65000,  # >= 64800 s (18 h) required
        },
    ]

    res = validate_dnv_switchboard_and_bus(
        earthing_system="MARINE_IT_440V",
        has_imd=True,
        msb_location_id="COMPARTMENT-MSB-DECK1",
        esb_location_id="COMPARTMENT-ESB-DECK4",
        essential_consumers=consumers,
        thd_voltage_pct=3.8,
    )
    assert res["compliant"] is True
    assert len(res["violations"]) == 0
    assert len(res["warnings"]) == 0
    assert res["thd_ok"] is True
    assert res["details"]["earthing_ok"] is True
    assert res["details"]["switchboard_separation_ok"] is True


def test_dnv_switchboard_prohibited_tnc_earthing() -> None:
    """Test rejection of TN-C PEN hull-return earthing system."""
    res = validate_dnv_switchboard_and_bus(
        earthing_system="TN-C",
        has_imd=True,
        msb_location_id="ROOM-MSB",
        esb_location_id="ROOM-ESB",
        essential_consumers=[],
        thd_voltage_pct=2.0,
    )
    assert res["compliant"] is False
    assert any("prohibited on marine vessels" in v for v in res["violations"])


def test_dnv_switchboard_it_system_without_imd() -> None:
    """Test rejection of insulated IT system lacking active IMD."""
    res = validate_dnv_switchboard_and_bus(
        earthing_system="MARINE_IT_230V",
        has_imd=False,
        msb_location_id="ROOM-MSB",
        esb_location_id="ROOM-ESB",
        essential_consumers=[],
        thd_voltage_pct=2.0,
    )
    assert res["compliant"] is False
    assert any("requires an active Insulation Monitoring Device" in v for v in res["violations"])


def test_dnv_switchboard_same_compartment_violation() -> None:
    """Test rejection when MSB and ESB share the same compartment/location."""
    res = validate_dnv_switchboard_and_bus(
        earthing_system="IT_230V_3P",
        has_imd=True,
        msb_location_id="ENGINE_ROOM_MAIN",
        esb_location_id="ENGINE_ROOM_MAIN",
        essential_consumers=[],
        thd_voltage_pct=2.0,
    )
    assert res["compliant"] is False
    assert any("distinct fire zones" in v for v in res["violations"])


def test_dnv_switchboard_essential_consumer_dual_feed_and_autonomy_violations() -> None:
    """Test essential consumers lacking dual feed or minimum UPS autonomy."""
    consumers = [
        {
            "name": "Fire Pump Primary",
            "service_type": "fire_pump",
            "feed_count": 1,
            "has_emergency_feed": False,
            "ups_autonomy_minutes": 15,  # Below 30 min
        },
        {
            "name": "Emergency Exit Lighting",
            "service_type": "emergency_lighting",
            "feed_count": 2,
            "has_emergency_feed": True,
            "ups_autonomy_hours": 4,  # Below 18 h
        },
    ]

    res = validate_dnv_switchboard_and_bus(
        earthing_system="MARINE_IT_690V",
        has_imd=True,
        msb_location_id="MSB_ROOM",
        esb_location_id="ESB_ROOM",
        essential_consumers=consumers,
        thd_voltage_pct=3.0,
    )
    assert res["compliant"] is False
    assert any("requires dual independent feeds" in v for v in res["violations"])
    assert any("UPS autonomy" in v and "18.0h" in v for v in res["violations"])


def test_dnv_switchboard_thd_limits() -> None:
    """Test THD warnings (>5%) and violations (>8%)."""
    # 6.0% THD should pass compliance but produce a warning
    res_warn = validate_dnv_switchboard_and_bus(
        earthing_system="MARINE_IT_440V",
        has_imd=True,
        msb_location_id="MSB_ROOM",
        esb_location_id="ESB_ROOM",
        essential_consumers=[],
        thd_voltage_pct=6.0,
    )
    assert res_warn["compliant"] is True
    assert len(res_warn["warnings"]) == 1
    assert "exceeds 5.0% general distribution limit" in res_warn["warnings"][0]

    # 9.5% THD should fail compliance
    res_fail = validate_dnv_switchboard_and_bus(
        earthing_system="MARINE_IT_440V",
        has_imd=True,
        msb_location_id="MSB_ROOM",
        esb_location_id="ESB_ROOM",
        essential_consumers=[],
        thd_voltage_pct=9.5,
    )
    assert res_fail["compliant"] is False
    assert any("exceeds maximum marine limit of 8.0%" in v for v in res_fail["violations"])


def test_validate_marine_cable_compliant() -> None:
    """Test marine cable meeting IEC 60092-350/360 SHF1/SHF2 and IEC 60332-3-22."""
    spec = {
        "cable_type": "RFOU 0.6/1kV",
        "sheath_type": "SHF2",
        "flame_retardant": True,
        "standards": "IEC 60092-353, IEC 60332-3-22",
    }
    res = validate_marine_cable_iec60092(spec, is_emergency_circuit=False)
    assert res["compliant"] is True
    assert res["halogen_free"] is True
    assert res["flame_retardant"] is True


def test_validate_marine_cable_pvc_violation() -> None:
    """Test rejection of non-halogen-free PVC cable sheath."""
    spec = {
        "cable_type": "NYY-J",
        "sheath_type": "PVC",
        "flame_retardant": True,
    }
    res = validate_marine_cable_iec60092(spec, is_emergency_circuit=False)
    assert res["compliant"] is False
    assert any("halogen-free (SHF1 or SHF2)" in v for v in res["violations"])


def test_validate_marine_cable_emergency_fire_resistance() -> None:
    """Test emergency circuit requiring IEC 60331 fire resistance (BFOU vs RFOU)."""
    # Non-fire-resistant RFOU cable on emergency circuit -> fails
    rfou = {
        "cable_type": "RFOU 2x1.5mm2",
        "sheath_type": "SHF2",
        "flame_retardant": True,
        "fire_resistant": False,
    }
    res_fail = validate_marine_cable_iec60092(rfou, is_emergency_circuit=True)
    assert res_fail["compliant"] is False
    assert any(
        "requires fire-resistant cable certified to IEC 60331" in v for v in res_fail["violations"]
    )

    # Fire-resistant BFOU cable on emergency circuit -> passes
    bfou = {
        "cable_type": "BFOU 2x1.5mm2",
        "sheath_type": "SHF2",
        "flame_retardant": True,
        "fire_resistant": True,
        "standards": "IEC 60331-21, IEC 60332-3-22",
    }
    res_pass = validate_marine_cable_iec60092(bfou, is_emergency_circuit=True)
    assert res_pass["compliant"] is True
    assert res_pass["fire_resistant"] is True


def test_validate_atex_and_environment_compliant() -> None:
    """Test compliant Ex device in Zone 1 with Ga/Gb EPL, T4 class, and outdoor marine ratings."""
    device = {
        "model": "Ex-Horn-100",
        "epl": "Gb",
        "temperature_class": "T4",
        "ip_rating": "IP66",
        "ik_rating": "IK08",
        "corrosion_class": "C5",
    }
    env = {
        "environment": "open_deck",
        "ex_zone": "zone_1",
        "required_temp_class": "T3",
    }
    res = validate_atex_and_environment(device, env)
    assert res["compliant"] is True
    assert len(res["violations"]) == 0


def test_validate_atex_epl_and_temp_class_violations() -> None:
    """Test ATEX EPL insufficiency and temperature class violation."""
    # Zone 0 requires Ga; Gc is insufficient
    device_bad_epl = {
        "atex_marking": "II 3 G Ex ec IIC T4 Gc",
        "ip_rating": "IP66",
        "ik_rating": "IK08",
        "corrosion_class": "C4",
    }
    env_zone_0 = {
        "environment": "wet_room",
        "ex_zone": "zone_0",
    }
    res = validate_atex_and_environment(device_bad_epl, env_zone_0)
    assert res["compliant"] is False
    assert any("Device EPL 'Gc' is insufficient for zone_0" in v for v in res["violations"])

    # Required T4; device is T2 -> surface temperature exceeds limit
    device_bad_t = {
        "epl": "Gb",
        "temperature_class": "T2",
        "ip_rating": "IP66",
        "ik_rating": "IK08",
        "corrosion_class": "C3",
    }
    env_t4 = {
        "environment": "machinery_space",
        "ex_zone": "zone_1",
        "required_temp_class": "T4",
    }
    res_t = validate_atex_and_environment(device_bad_t, env_t4)
    assert res_t["compliant"] is False
    assert any(
        "temperature class 'T2' is less protective than required 'T4'" in v
        for v in res_t["violations"]
    )


def test_validate_environment_ip_ik_corrosion_violations() -> None:
    """Test environmental ratings: IP rating, IK impact, and ISO 12944 corrosivity."""
    device = {
        "ip_rating": "IP44",  # Requires IP66
        "ik_rating": "IK04",  # Requires IK10
        "corrosion_class": "C3",  # Requires CX
    }
    env = {
        "environment": "offshore_splash_zone",
    }
    res = validate_atex_and_environment(device, env)
    assert res["compliant"] is False
    assert any(
        "Device IP rating 'IP44' does not meet required 'IP66'" in v for v in res["violations"]
    )
    assert any("Device IK impact rating 'IK04'" in v for v in res["violations"])
    assert any("Device corrosion protection class 'C3'" in v for v in res["violations"])


def test_validate_dnv_location_classes() -> None:
    """Test DNV-CG-0339 location test classes (Temperature, Vibration, Enclosure, etc.)."""
    # Compliant device
    dev_classes = {
        "Temperature": "D",
        "Humidity": "B",
        "Vibration": "B",
        "EMC": "B",
        "Enclosure": "C",
    }
    req_classes = {
        "Temperature": "C",
        "Humidity": "B",
        "Vibration": "A",
        "EMC": "B",
        "Enclosure": "B",
    }
    res = validate_dnv_location_classes(dev_classes, req_classes)
    assert res["compliant"] is True
    assert len(res["violations"]) == 0

    # Non-compliant device failing vibration
    dev_failing = {
        "Temperature": "C",
        "Vibration": "A",  # Required is B
    }
    req_vibration_b = {
        "Temperature": "B",
        "Vibration": "B",
    }
    res_fail = validate_dnv_location_classes(dev_failing, req_vibration_b)
    assert res_fail["compliant"] is False
    assert any(
        "Vibration class 'A' does not satisfy required class 'B'" in v
        for v in res_fail["violations"]
    )


def test_evaluate_regulatory_compliance_dnv_and_atex() -> None:
    """Test evaluate_regulatory_compliance helper for DNV TAC, ATEX, and ISO 12944."""
    check_date = date(2026, 10, 16)

    # 1. Fully valid certificates
    valid_params = {
        "dnv_tac_number": "TAA0000123",
        "dnv_tac_expiry": "2028-06-30",
        "atex_marking": "II 2 G Ex db IIC T4 Gb",
        "atex_cert_number": "Presafe 18 ATEX 12345X",
        "atex_cert_expiry": "2029-01-01",
        "iso12944_class": "CX",
        "ip_rating": "IP66",
    }
    res = evaluate_regulatory_compliance(valid_params, check_date)
    assert res["compliant"] is True
    assert res["dnv_tac"]["valid"] is True
    assert res["dnv_tac"]["is_expired"] is False
    assert res["dnv_tac"]["days_remaining"] > 0
    assert res["atex"]["valid"] is True
    assert res["environmental"]["valid"] is True

    # 2. Expired DNV TAC
    expired_params = {
        "dnv_tac_number": "TAA0000099",
        "dnv_tac_expiry": "2025-12-31",  # Past check_date 2026-10-16
        "atex_marking": "II 2 G Ex db IIC T4 Gb",
    }
    res_exp = evaluate_regulatory_compliance(expired_params, check_date)
    assert res_exp["compliant"] is False
    assert res_exp["dnv_tac"]["is_expired"] is True
    assert any("expired on 2025-12-31" in v for v in res_exp["violations"])

    # 3. Invalid ATEX and ISO classes
    invalid_params = {
        "atex_marking": "NOT_AN_EX_STRING",
        "iso12944_class": "INVALID_CLASS",
        "ip_rating": "BAD_IP",
    }
    res_inv = evaluate_regulatory_compliance(invalid_params, check_date)
    assert res_inv["compliant"] is False
    assert any("Invalid or unrecognized ATEX marking" in v for v in res_inv["violations"])
    assert any("Invalid ISO 12944 corrosion category" in v for v in res_inv["violations"])
    assert any("Invalid EN 60529 IP rating format" in v for v in res_inv["violations"])


@pytest.mark.asyncio
async def test_warranty_eol_with_regulatory_compliance() -> None:
    """Test do_check_warranty_eol returns regulatory_compliance key when regulatory params are supplied."""
    engine, conn = _make_engine(fetch_return=[])

    # Case A: Without regulatory params -> regulatory_compliance NOT in response
    res_plain = await do_check_warranty_eol(engine, {"namespace_id": _NAMESPACE_ID})
    assert res_plain["ok"] is True
    assert "regulatory_compliance" not in res_plain

    # Case B: With DNV TAC & ATEX params -> regulatory_compliance present and evaluated
    params = {
        "namespace_id": _NAMESPACE_ID,
        "dnv_tac_number": "TAA0000ABC",
        "dnv_tac_expiry": "2027-12-31",
        "atex_marking": "II 2 G Ex eb IIC T5 Gb",
        "iso12944_class": "C5",
        "now": "2026-10-16T12:00:00Z",
    }
    res_reg = await do_check_warranty_eol(engine, params)
    assert res_reg["ok"] is True
    assert "regulatory_compliance" in res_reg
    reg = res_reg["regulatory_compliance"]
    assert reg["compliant"] is True
    assert reg["dnv_tac"]["certificate_number"] == "TAA0000ABC"
    assert reg["dnv_tac"]["is_expired"] is False
