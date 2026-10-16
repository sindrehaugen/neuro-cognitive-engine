"""
tests/unit/test_inventory_foundation.py
=======================================
Unit tests for Wave E-6:
  - Electronics QC (ECSS / IEC 61508 / ISO 26262 Component Derating)
  - EU Electronics Compliance (RoHS 3 / REACH / CE / IPC-JEDEC J-STD-033 MSL)
  - EN 61340-5-1 ESD Protected Area (EPA) Workstation Qualification
  - Field Tech Scan Component Lot/Reel Traceability & used_in KG Edge
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from nce.vertical_modules.field_tech.scan import do_scan_serial
from nce.vertical_modules.system_design.electronics_qc import (
    validate_component_derating,
    validate_en61340_esd_workstation,
    validate_eu_electronics_compliance,
)

_NS_ID = "11111111-1111-4111-8111-111111111111"
_WO_ID = "WO-E6-001"


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value = _AsyncCtx(conn)
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))
    return pool


# ===========================================================================
# 1. Component Derating Tests (ECSS / IEC 61508 / ISO 26262)
# ===========================================================================


def test_derating_resistor_industrial_and_automotive() -> None:
    """Test resistor power derating limits (0.50 industrial, 0.40 automotive)."""
    # 0.22W on 0.5W rated -> ratio 0.44
    resistors = [
        {
            "part_number": "R-100K-0805",
            "type": "resistor",
            "power_applied": 0.22,
            "power_rated": 0.50,
        }
    ]

    # In industrial, 0.44 <= 0.50 -> PASS
    res_ind = validate_component_derating(resistors, safety_class="industrial")
    assert res_ind["passed"] is True
    assert res_ind["compliant_count"] == 1
    assert res_ind["violation_count"] == 0

    # In automotive_iso26262, max allowed is 0.40 -> 0.44 > 0.40 -> FAIL
    res_auto = validate_component_derating(resistors, safety_class="automotive_iso26262")
    assert res_auto["passed"] is False
    assert res_auto["violation_count"] == 1
    assert res_auto["violations"][0]["parameter"] == "power"
    assert res_auto["violations"][0]["max_allowed_ratio"] == 0.40


def test_derating_capacitor_voltage() -> None:
    """Test capacitor voltage derating limits (0.60 industrial, 0.50 tightened)."""
    # 14V applied on 25V rated capacitor -> 14 / 25 = 0.56
    caps = [
        {
            "part_number": "C-10UF-25V",
            "type": "capacitor",
            "voltage_applied": 14.0,
            "voltage_rated": 25.0,
        }
    ]

    # Industrial allows 0.60 -> 0.56 <= 0.60 -> PASS
    res_ind = validate_component_derating(caps, safety_class="industrial")
    assert res_ind["passed"] is True

    # Safety IEC 61508 tightened allows 0.50 -> 0.56 > 0.50 -> FAIL
    res_safety = validate_component_derating(caps, safety_class="safety_iec61508")
    assert res_safety["passed"] is False
    assert res_safety["violations"][0]["parameter"] == "voltage"
    assert res_safety["violations"][0]["max_allowed_ratio"] == 0.50


def test_derating_semiconductor_voltage_and_power() -> None:
    """Test semiconductor voltage and power derating."""
    semis = [
        {
            "part_number": "Q-MOSFET-N",
            "type": "semiconductor",
            "v_applied": 40.0,
            "v_rated": 60.0,  # ratio = 0.667 <= 0.75 (industrial pass)
            "p_applied": 1.2,
            "p_rated": 2.0,  # ratio = 0.60 <= 0.70 (industrial pass)
        }
    ]
    res_ind = validate_component_derating(semis, safety_class="industrial")
    assert res_ind["passed"] is True

    # Maritime DNV tightened: max V ratio = 0.65, max P ratio = 0.60
    # 40/60 = 0.667 > 0.65 -> voltage violation
    res_mar = validate_component_derating(semis, safety_class="maritime_dnv")
    assert res_mar["passed"] is False
    assert any(v["parameter"] == "voltage" for v in res_mar["violations"])


def test_derating_thermal_junction_temperature() -> None:
    """Test thermal junction calculation: Tj = Ta + P * R_theta_ja <= Tj_max - margin."""
    # Ta = 40Â°C, P = 1.0W, R_theta_ja = 60Â°C/W -> Tj = 40 + 60 = 100Â°C.
    # Tj_max = 125Â°C.
    # Industrial margin = 15Â°C -> max allowable = 110Â°C -> 100 <= 110 -> PASS.
    # Safety margin = 25Â°C -> max allowable = 100Â°C -> 100 <= 100 -> PASS.
    # If P = 1.2W -> Tj = 40 + 72 = 112Â°C -> fails both.
    comp_pass = [
        {
            "part_number": "IC-LDO-5V",
            "type": "ic",
            "t_ambient_c": 40.0,
            "power_applied": 1.0,
            "power_rated": 3.0,
            "r_theta_ja": 60.0,
            "tj_max": 125.0,
        }
    ]
    res_pass = validate_component_derating(comp_pass, safety_class="industrial")
    assert res_pass["passed"] is True
    assert res_pass["details"][0]["t_junction_c"] == 100.0

    comp_fail = [
        {
            "part_number": "IC-LDO-5V-HOT",
            "type": "ic",
            "t_ambient_c": 40.0,
            "power_applied": 1.2,
            "power_rated": 3.0,
            "r_theta_ja": 60.0,
            "tj_max": 125.0,
        }
    ]
    res_fail = validate_component_derating(comp_fail, safety_class="industrial")
    assert res_fail["passed"] is False
    assert any(v["parameter"] == "thermal_junction" for v in res_fail["violations"])


def test_derating_nonpositive_ratings_flagged() -> None:
    """Zero or negative rated values are flagged as violations."""
    bad_comp = [
        {
            "part_number": "BAD-CAP",
            "type": "capacitor",
            "voltage_applied": 5.0,
            "voltage_rated": 0.0,
        }
    ]
    res = validate_component_derating(bad_comp)
    assert res["passed"] is False
    assert res["violations"][0]["stress_ratio"] == math.inf


# ===========================================================================
# 2. EU Electronics Compliance & MSL Floor Life Tests
# ===========================================================================


def test_eu_compliance_clean_bom_passes() -> None:
    """Fully compliant BOM passes RoHS 3, REACH, and MSL checks."""
    bom = [
        {
            "part_number": "C0805C104K5RACTU",
            "rohs_compliant": True,
            "reach_compliant": True,
            "msl_level": "MSL 1",
            "exposure_hours": 1000.0,  # MSL 1 is unlimited
        },
        {
            "part_number": "STM32F407VGT6",
            "rohs_compliant": True,
            "reach_compliant": True,
            "msl_level": "3",
            "exposure_hours": 72.0,  # MSL 3 max is 168h -> PASS
        },
    ]
    res = validate_eu_electronics_compliance(bom, require_rohs=True, require_reach=True)
    assert res["passed"] is True
    assert res["compliant_count"] == 2
    assert res["violation_count"] == 0
    assert len(res["violations"]) == 0


def test_eu_compliance_rohs_substance_thresholds() -> None:
    """Lead (Pb) > 1000 ppm and Cadmium (Cd) > 100 ppm are detected as violations."""
    bom = [
        {
            "part_number": "LEADED-SOLDER-RESISTOR",
            "rohs_compliant": True,
            "substances": {"Pb": 1500.0},  # Exceeds 1000 ppm
        },
        {
            "part_number": "CADMIUM-PLATED-CONNECTOR",
            "substances": {"Cd": 150.0},  # Exceeds 100 ppm
        },
        {
            "part_number": "PHTHALATE-CABLE",
            "substances": {"DEHP": 0.002},  # 0.2% exceeds 0.1% max
        },
    ]
    res = validate_eu_electronics_compliance(bom)
    assert res["passed"] is False
    assert res["violation_count"] == 3
    assert len(res["violations"]) == 3
    substances_flagged = {v.get("substance") for v in res["violations"]}
    assert "Pb" in substances_flagged
    assert "Cd" in substances_flagged
    assert "DEHP" in substances_flagged


def test_eu_compliance_reach_svhc() -> None:
    """REACH SVHC exceeding threshold is flagged."""
    bom = [
        {
            "part_number": "HIGH-SVHC-POLYMER",
            "reach_svhc_ppm": 2500.0,  # Exceeds 1000 ppm
        }
    ]
    res = validate_eu_electronics_compliance(bom, require_reach=True)
    assert res["passed"] is False
    assert any(v["standard"].startswith("REACH") for v in res["violations"])


def test_eu_compliance_ce_marking() -> None:
    """CE non-compliance is flagged when require_ce=True."""
    bom = [{"part_number": "MODULE-A", "ce_compliant": False}]
    res = validate_eu_electronics_compliance(bom, require_ce=True)
    assert res["passed"] is False
    assert any(v["issue"] == "ce_non_compliant" for v in res["violations"])


def test_msl_floor_life_and_level_6_baking() -> None:
    """IPC/JEDEC J-STD-033 MSL floor life and Level 6 baking requirements."""
    bom = [
        {
            "part_number": "QFP-144-EXPIRED",
            "msl_level": "MSL 3",
            "exposure_hours": 200.0,  # Max is 168h -> FAIL
        },
        {
            "part_number": "BGA-UNBAKED-MSL6",
            "msl_level": "MSL 6",
            "baked_before_use": False,  # Mandatory bake -> FAIL
        },
        {
            "part_number": "BGA-BAKED-MSL6",
            "msl_level": "MSL 6",
            "baked_before_use": True,  # Properly baked -> PASS
        },
    ]
    res = validate_eu_electronics_compliance(bom)
    assert res["passed"] is False
    assert res["violation_count"] == 2
    assert res["compliant_count"] == 1


# ===========================================================================
# 3. EN 61340-5-1 ESD Workstation Validation Tests
# ===========================================================================


def test_esd_workstation_compliant() -> None:
    """A compliant EPA workstation passes all resistance and humidity limits."""
    res = validate_en61340_esd_workstation(
        wrist_strap_mohm=2.2,
        mat_to_ground_ohm=1.0e7,
        footwear_ohm=5.0e6,
        relative_humidity_pct=45.0,
    )
    assert res["passed"] is True
    assert len(res["violations"]) == 0
    assert len(res["warnings"]) == 0


def test_esd_wrist_strap_limits() -> None:
    """Wrist strap fails if < 0.75 MÎ© (shock hazard) or > 35 MÎ© (no discharge)."""
    # Too low
    res_low = validate_en61340_esd_workstation(wrist_strap_mohm=0.5, mat_to_ground_ohm=1.0e6)
    assert res_low["passed"] is False
    assert any("below minimum safety limit" in v for v in res_low["violations"])

    # Too high
    res_high = validate_en61340_esd_workstation(wrist_strap_mohm=40.0, mat_to_ground_ohm=1.0e6)
    assert res_high["passed"] is False
    assert any("exceeds maximum dissipation limit" in v for v in res_high["violations"])


def test_esd_mat_and_footwear_limits() -> None:
    """Worksurface mat >= 1 GÎ© and footwear >= 100 MÎ© fail."""
    res = validate_en61340_esd_workstation(
        wrist_strap_mohm=1.5,
        mat_to_ground_ohm=1.5e9,  # > 1.0e9 limit
        footwear_ohm=2.0e8,  # > 1.0e8 limit
    )
    assert res["passed"] is False
    assert len(res["violations"]) == 2


def test_esd_low_relative_humidity() -> None:
    """Relative humidity < 30% triggers warning and violation."""
    res = validate_en61340_esd_workstation(
        wrist_strap_mohm=1.5,
        mat_to_ground_ohm=1.0e6,
        relative_humidity_pct=22.0,
    )
    assert res["passed"] is False
    assert len(res["warnings"]) == 1
    assert "triboelectric" in res["warnings"][0]


# ===========================================================================
# 4. Field Tech Scan Traceability Tests
# ===========================================================================


def _make_mock_scan_conn() -> AsyncMock:
    conn = AsyncMock()

    async def mock_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "work_orders" in query:
            return {"id": UUID(_NS_ID), "location_id": "SITE:LAB:BENCH-1"}
        if "node_ownership_registry" in query:
            return {"owner_engine": "field_tech"}
        if "bom_line_content" in query:
            return {"quote_id": "Q-01", "line_ref": "L-01"}
        if "bom_lines" in query:
            return {
                "quote_id": "Q-01",
                "line_ref": "L-01",
                "status": "INSTALLED",
                "owner_engine": "field_tech",
            }
        return {"owner_engine": "field_tech", "id": UUID(_NS_ID), "location_id": "SITE:LAB:BENCH-1"}

    conn.fetchrow.side_effect = mock_fetchrow
    conn.execute = AsyncMock(return_value="INSERT 1")
    return conn


@pytest.mark.asyncio
async def test_do_scan_serial_with_traceability_and_lot_edge() -> None:
    """do_scan_serial records traceability fields and inserts LOT used_in edge."""
    conn = _make_mock_scan_conn()
    pool = _make_mock_pool(conn)

    res = await do_scan_serial(
        pool,
        {
            "namespace_id": _NS_ID,
            "work_order_id": _WO_ID,
            "bom_line_id": "BOM-PCBA-01",
            "serial": "SN-PCBA-9001",
            "lot_code": "LOT-2026-W42",
            "date_code": "2642",
            "reel_id": "REEL-4482",
            "msl_level": "MSL 3",
            "rohs_compliant": True,
            "record_lot_edge": True,
        },
    )

    assert res["status"] == "scanned"
    assert res["serial"] == "SN-PCBA-9001"
    assert "traceability" in res
    assert res["traceability"]["lot_code"] == "LOT-2026-W42"
    assert res["traceability"]["date_code"] == "2642"
    assert res["traceability"]["reel_id"] == "REEL-4482"
    assert res["traceability"]["msl_level"] == "3"
    assert res["traceability"]["rohs_compliant"] is True

    assert "lot_edge" in res
    assert res["lot_edge"]["subject"] == "LOT:LOT-2026-W42"
    assert res["lot_edge"]["predicate"] == "used_in"
    assert res["lot_edge"]["object"] == "ASSET:SN-PCBA-9001"

    # Verify edge was executed in kg_edges
    lot_edge_calls = [
        c
        for c in conn.execute.call_args_list
        if "INSERT INTO kg_edges" in str(c[0][0]) and "used_in" in str(c[0][0])
    ]
    assert len(lot_edge_calls) == 1
    call_args = lot_edge_calls[0][0]
    assert call_args[1] == "LOT:LOT-2026-W42"
    assert call_args[2] == "ASSET:SN-PCBA-9001"


@pytest.mark.asyncio
async def test_do_scan_serial_traceability_invalid_msl() -> None:
    """do_scan_serial raises ValueError on invalid msl_level."""
    conn = _make_mock_scan_conn()
    pool = _make_mock_pool(conn)

    with pytest.raises(ValueError, match="Invalid msl_level"):
        await do_scan_serial(
            pool,
            {
                "namespace_id": _NS_ID,
                "work_order_id": _WO_ID,
                "bom_line_id": "BOM-01",
                "serial": "SN-01",
                "msl_level": "MSL 99",
            },
        )


@pytest.mark.asyncio
async def test_do_scan_serial_record_lot_edge_requires_lot_code() -> None:
    """do_scan_serial raises ValueError if record_lot_edge=True but lot_code missing."""
    conn = _make_mock_scan_conn()
    pool = _make_mock_pool(conn)

    with pytest.raises(ValueError, match="lot_code is required"):
        await do_scan_serial(
            pool,
            {
                "namespace_id": _NS_ID,
                "work_order_id": _WO_ID,
                "bom_line_id": "BOM-01",
                "serial": "SN-01",
                "record_lot_edge": True,
            },
        )
