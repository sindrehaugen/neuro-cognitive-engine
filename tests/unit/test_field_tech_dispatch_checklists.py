"""
tests/unit/test_field_tech_dispatch_checklists.py
==================================================
Unit tests for Module 12 (Field Tech Engine) Wave E-3:
  - Regulatory checklist templates (FSE / EN 50110-1 SJA, NEK 400 / EN 61557 Sluttkontroll,
    NEK 700 EKOM, DNV Marine HAT/SAT, ISO 9001/14001/45001/50001, IPC-A-610/620 ESD QC)
  - Quantitative threshold bound validation (min_value, max_value, unit) and measurement violations
  - Contract return keys preservation (work_order_id, kind, passed, missing_required, completed_count, total_count, memory_id)
  - Norwegian Samsvarserklæring generator (FEL § 12 / NEK 400 / NEK 700) with cryptographic signing and fail-closed gates
  - EU Declaration of Conformity (CE Marking) generator per EU Directives (LVD, EMC, Machinery, ATEX, RoHS)
  - AI dispatch rework penalization regression test
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from nce.vertical_modules.field_tech.checklist import (
    ChecklistIncompleteError,
    _load_template_items,
    _load_template_kind,
    do_complete_checklist,
    do_submit_checklist,
)
from nce.vertical_modules.field_tech.dispatch import do_dispatch
from nce.vertical_modules.field_tech.samsvar import (
    generate_eu_declaration_of_conformity,
    generate_samsvarserklaering,
)

_NS_A = "00000000-0000-4000-8000-000000000001"
_WO_ID_1 = "WO-2026-NEK400-01"


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
    return pool


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _fake_scoped(pool: Any, ns: Any) -> Any:
        ctx = pool.acquire()
        if hasattr(ctx, "__aenter__"):
            conn = await ctx.__aenter__()
            try:
                yield conn
            finally:
                await ctx.__aexit__(None, None, None)
        else:
            yield ctx

    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.checklist.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.dispatch.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    mock_assert_owner = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.checklist.assert_owner",
        mock_assert_owner,
        raising=False,
    )


# ============================================================================
# 1. AI Dispatch Rework Penalization Test
# ============================================================================


@pytest.mark.asyncio
async def test_dispatch_penalizes_technician_rework_history() -> None:
    """Technician with high rework rate has quality score discounted vs clean history."""
    ns_id = str(uuid4())
    wo_id = "WO-REWORK-001"

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": wo_id,
        "namespace_id": UUID(ns_id),
        "kind": "service",
        "location_id": "LOC-OSLO",
        "raw": {"required_skills": ["cctv"]},
    }

    conn.fetch.side_effect = [
        [
            {"assignee_id": "tech_clean", "open_count": 0},
            {"assignee_id": "tech_rework", "open_count": 0},
        ],
        [
            {
                "completed_by": "tech_clean",
                "completed_count": 5,
                "avg_rating": 4.8,
                "avg_quality": 0.95,
                "rework_count": 0,
            },
            {
                "completed_by": "tech_rework",
                "completed_count": 5,
                "avg_rating": 4.8,
                "avg_quality": 0.95,
                "rework_count": 3,
            },
        ],
    ]

    pool = _make_mock_pool(conn)
    candidates = [
        {"id": "tech_clean", "name": "Clean Tech", "skills": ["cctv"], "location_id": "LOC-OSLO"},
        {"id": "tech_rework", "name": "Rework Tech", "skills": ["cctv"], "location_id": "LOC-OSLO"},
    ]

    res = await do_dispatch(
        pool,
        {
            "namespace_id": ns_id,
            "work_order_id": wo_id,
            "candidates": candidates,
        },
    )

    ranked = res["ranked_candidates"]
    assert len(ranked) == 2
    assert ranked[0]["id"] == "tech_clean"
    assert ranked[1]["id"] == "tech_rework"
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["component_scores"]["history"] > ranked[1]["component_scores"]["history"]
    assert "reworks" in ranked[1]["rationale"]


# ============================================================================
# 2. Template Definitions & Quant Bounds Inspection
# ============================================================================


def test_regulatory_checklist_templates_exist() -> None:
    """Verify all 8 checklist templates exist with correct required items and bounds."""
    for tmpl_id in (
        "install_standard",
        "service_dispatch",
        "fse_en50110_sja",
        "nek400_en61557_sluttkontroll",
        "nek700_en50174_ekom",
        "dnv_hat_sat_marine",
        "iso_9001_14001_45001_handover",
        "ipc_a610_a620_esd_qc",
    ):
        items = _load_template_items(tmpl_id)
        assert len(items) > 0, f"Template {tmpl_id} returned no items"
        assert _load_template_kind(tmpl_id) != ""

    # Check specific NEK 400 bounds
    nek_items = {it["id"]: it for it in _load_template_items("nek400_en61557_sluttkontroll")}
    assert "pe_continuity_ohm" in nek_items
    assert nek_items["pe_continuity_ohm"]["max_value"] == 1.0
    assert nek_items["pe_continuity_ohm"]["unit"] == "ohm"

    assert "insulation_resistance_mohm" in nek_items
    assert nek_items["insulation_resistance_mohm"]["min_value"] == 1.0
    assert nek_items["insulation_resistance_mohm"]["unit"] == "MOhm"

    assert "rcd_trip_time_ms" in nek_items
    assert nek_items["rcd_trip_time_ms"]["max_value"] == 300.0
    assert nek_items["rcd_trip_time_ms"]["unit"] == "ms"

    # Check FSE 5 security rules
    fse_items = {it["id"]: it for it in _load_template_items("fse_en50110_sja")}
    assert "fse_1_disconnect" in fse_items
    assert "fse_2_loto" in fse_items
    assert "fse_3_verify_dead" in fse_items
    assert "fse_4_earth_short" in fse_items
    assert "fse_5_shield_adjacent" in fse_items
    assert "sja_ppe_arc_flash" in fse_items


# ============================================================================
# 3. do_submit_checklist Quantitative Bounds & Contract Keys
# ============================================================================


@pytest.mark.asyncio
async def test_do_submit_checklist_nek400_success() -> None:
    """NEK 400 Sluttkontroll passes when all measurements satisfy quantitative thresholds."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"partner_scope_id": None},
        {
            "id": UUID("11111111-2222-3333-4444-555555555555"),
            "checklist_id": "CL-NEK400-01",
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "template_id": "nek400_en61557_sluttkontroll",
            "items": "[]",
            "completed_at": None,
            "raw": "{}",
            "created_at": None,
            "updated_at": None,
        },
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    items = [
        {"id": "visual_inspection", "ticked": True},
        {"id": "pe_continuity_ohm", "ticked": True, "measured_value": 0.18},
        {"id": "insulation_resistance_mohm", "ticked": True, "measured_value": 250.0},
        {"id": "rcd_trip_time_ms", "ticked": True, "measured_value": 22.4},
        {"id": "fault_loop_impedance_verified", "ticked": True},
        {"id": "polarity_and_phase_sequence", "ticked": True},
    ]

    res = await do_submit_checklist(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "template_id": "nek400_en61557_sluttkontroll",
            "checklist_id": "CL-NEK400-01",
            "items": items,
        },
    )

    assert res["passed"] is True
    assert res["is_complete"] is True
    assert res["measurement_violations"] == []
    assert res["missing_required"] == []
    assert res["completed_count"] == 6
    assert res["total_count"] == 6
    assert res["work_order_id"] == _WO_ID_1
    assert res["kind"] == "sluttkontroll"
    assert res["memory_id"] == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_do_submit_checklist_detects_quantitative_violations() -> None:
    """NEK 400 Sluttkontroll records violations and sets passed=False if measurements exceed bounds."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"partner_scope_id": None},
        {
            "id": UUID("11111111-2222-3333-4444-555555555555"),
            "checklist_id": "CL-NEK400-02",
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "template_id": "nek400_en61557_sluttkontroll",
            "items": "[]",
            "completed_at": None,
            "raw": "{}",
            "created_at": None,
            "updated_at": None,
        },
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    # Violations:
    # pe_continuity_ohm: 2.5 > max 1.0 ohm
    # insulation_resistance_mohm: 0.35 < min 1.0 MOhm
    # rcd_trip_time_ms: 380.0 > max 300.0 ms
    items = [
        {"id": "visual_inspection", "ticked": True},
        {"id": "pe_continuity_ohm", "ticked": True, "measured_value": 2.5},
        {"id": "insulation_resistance_mohm", "ticked": True, "measured_value": 0.35},
        {"id": "rcd_trip_time_ms", "ticked": True, "value": 380.0},
        {"id": "fault_loop_impedance_verified", "ticked": True},
        {"id": "polarity_and_phase_sequence", "ticked": True},
    ]

    res = await do_submit_checklist(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "template_id": "nek400_en61557_sluttkontroll",
            "items": items,
        },
    )

    assert res["passed"] is False
    assert res["is_complete"] is True  # All items were ticked, but measurements failed
    assert len(res["measurement_violations"]) == 3
    violated_ids = [v["item_id"] for v in res["measurement_violations"]]
    assert "pe_continuity_ohm" in violated_ids
    assert "insulation_resistance_mohm" in violated_ids
    assert "rcd_trip_time_ms" in violated_ids


@pytest.mark.asyncio
async def test_do_submit_checklist_detects_missing_required() -> None:
    """Missing required checklist items fail completion gate."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"partner_scope_id": None},
        {
            "id": UUID("11111111-2222-3333-4444-555555555555"),
            "checklist_id": "CL-FSE-01",
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "template_id": "fse_en50110_sja",
            "items": "[]",
            "completed_at": None,
            "raw": "{}",
            "created_at": None,
            "updated_at": None,
        },
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    # Omit fse_3_verify_dead and fse_4_earth_short
    items = [
        {"id": "fse_1_disconnect", "ticked": True},
        {"id": "fse_2_loto", "ticked": True},
        {"id": "fse_3_verify_dead", "ticked": False},
        {"id": "fse_4_earth_short", "ticked": False},
        {"id": "fse_5_shield_adjacent", "ticked": True},
        {"id": "sja_ppe_arc_flash", "ticked": True},
    ]

    res = await do_submit_checklist(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "template_id": "fse_en50110_sja",
            "items": items,
        },
    )

    assert res["passed"] is False
    assert res["is_complete"] is False
    assert set(res["missing_required"]) == {"fse_3_verify_dead", "fse_4_earth_short"}
    assert res["completed_count"] == 4
    assert res["total_count"] == 6

    # Strict gate raises ChecklistIncompleteError
    with pytest.raises(ChecklistIncompleteError, match="missing required items"):
        await do_submit_checklist(
            pool,
            {
                "namespace_id": _NS_A,
                "work_order_id": _WO_ID_1,
                "template_id": "fse_en50110_sja",
                "items": items,
                "require_all_required": True,
            },
        )


@pytest.mark.asyncio
async def test_do_complete_checklist_delegates_to_submit() -> None:
    """do_complete_checklist preserves 100% backwards compatibility and returns contract keys."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"partner_scope_id": None},
        {
            "id": UUID("11111111-2222-3333-4444-555555555555"),
            "checklist_id": "CL-LEGACY-01",
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "template_id": "install_standard",
            "items": "[]",
            "completed_at": None,
            "raw": "{}",
            "created_at": None,
            "updated_at": None,
        },
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_complete_checklist(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "template_id": "install_standard",
            "items": [{"id": "pre_site_inspection", "ticked": True}],
        },
    )

    assert "work_order_id" in res
    assert "kind" in res
    assert "passed" in res
    assert "missing_required" in res
    assert "completed_count" in res
    assert "total_count" in res
    assert "memory_id" in res
    assert "is_complete" in res
    assert "measurement_violations" in res


# ============================================================================
# 4. Samsvarserklæring Generator Tests
# ============================================================================


def test_generate_samsvarserklaering_success() -> None:
    """Norwegian Samsvarserklæring generates compliant document with deterministic hash and signature."""
    params = {
        "work_order_id": "WO-EL-2026-99",
        "enterprise_name": "Nordic AV & Elektro AS",
        "enterprise_org_nr": "987654321",
        "dsb_elvirksomhet_id": "ELV-44210",
        "faglig_ansvarlig": "Ola Nordmann (Installatør)",
        "executing_installer": "Kari Montør",
        "installation_address": "Dronning Eufemias gate 16, 0191 Oslo",
        "gnr_bnr": "233/45",
        "boligmappa_plant_id": "BM-PLANT-9912",
        "earthing_system": "TN-C-S",
        "applied_standards": ["FEL § 12", "NEK 400:2022", "NEK 700:2024"],
        "sluttkontroll_summary": {
            "passed": True,
            "r_pe_max_ohm": 0.12,
            "r_iso_min_mohm": 320.0,
            "rcd_trip_ms": 24.1,
        },
        "kursfortegnelse_ref": "KF-DOC-2026-001",
        "fdv_documents": ["FDV-Main-Board.pdf", "FDV-AV-Rack.pdf"],
        "issued_at": "2026-10-16T12:00:00+00:00",
    }

    doc = generate_samsvarserklaering(params)

    assert doc["work_order_id"] == "WO-EL-2026-99"
    assert doc["enterprise_name"] == "Nordic AV & Elektro AS"
    assert doc["faglig_ansvarlig"] == "Ola Nordmann (Installatør)"
    assert doc["earthing_system"] == "TN-C-S"
    assert "FEL § 12" in doc["applied_standards"]
    assert "content_hash" in doc
    assert len(doc["content_hash"]) == 64  # SHA-256 hex
    assert "signature" in doc
    assert len(doc["signature"]) > 0

    # Determinism: same input gives identical content hash
    doc2 = generate_samsvarserklaering(params)
    assert doc["content_hash"] == doc2["content_hash"]


def test_generate_samsvarserklaering_fails_closed_on_missing_mandatory() -> None:
    """Missing mandatory fields fail closed with ValueError."""
    # Missing work_order_id
    with pytest.raises(ValueError, match="work_order_id is required"):
        generate_samsvarserklaering(
            {
                "enterprise_name": "Test AS",
                "faglig_ansvarlig": "Ola",
            }
        )

    # Missing enterprise_name
    with pytest.raises(ValueError, match="enterprise_name is required"):
        generate_samsvarserklaering(
            {
                "work_order_id": "WO-1",
                "faglig_ansvarlig": "Ola",
            }
        )

    # Missing faglig_ansvarlig
    with pytest.raises(ValueError, match="faglig_ansvarlig is required"):
        generate_samsvarserklaering(
            {
                "work_order_id": "WO-1",
                "enterprise_name": "Test AS",
            }
        )


def test_generate_samsvarserklaering_refuses_when_sluttkontroll_failed() -> None:
    """Samsvarserklæring strictly refuses generation if sluttkontroll did not pass."""
    # 1. Explicit sluttkontroll_passed is False
    with pytest.raises(ValueError, match="sluttkontroll has failed"):
        generate_samsvarserklaering(
            {
                "work_order_id": "WO-1",
                "enterprise_name": "Test AS",
                "faglig_ansvarlig": "Ola",
                "sluttkontroll_passed": False,
            }
        )

    # 2. sluttkontroll_summary has passed=False
    with pytest.raises(ValueError, match="sluttkontroll_summary indicates failure"):
        generate_samsvarserklaering(
            {
                "work_order_id": "WO-1",
                "enterprise_name": "Test AS",
                "faglig_ansvarlig": "Ola",
                "sluttkontroll_summary": {"passed": False, "reason": "Insulation failure"},
            }
        )


# ============================================================================
# 5. EU Declaration of Conformity (CE Marking) Tests
# ============================================================================


def test_generate_eu_declaration_of_conformity_success() -> None:
    """EU DoC builds European CE Marking document with directives and cryptographic hash."""
    params = {
        "work_order_id": "WO-EU-2026-01",
        "manufacturer_name": "Steps Systems Europe AS",
        "manufacturer_address": "Karenslyst Allé 10, 0278 Oslo, Norway",
        "product_identification": {
            "product_name": "Modular AV Power & Control Rack 42U",
            "model": "STEPS-RACK-42U",
            "serial_number": "SN-2026-EU-0012",
        },
        "signatory": {
            "name": "Dr. Erik Vang",
            "title": "Chief Technology Officer / Faglig Ansvarlig",
            "place": "Oslo, Norway",
            "date": "2026-10-16",
        },
        "machinery_applicable": True,
        "atex_applicable": False,
    }

    doc = generate_eu_declaration_of_conformity(params)

    assert doc["manufacturer_name"] == "Steps Systems Europe AS"
    assert doc["signatory"]["name"] == "Dr. Erik Vang"
    assert doc["ce_marking_year"] == 2026

    directive_ids = [d["directive"] for d in doc["directives"]]
    assert "2014/35/EU" in directive_ids  # Low Voltage
    assert "2014/30/EU" in directive_ids  # EMC
    assert "2011/65/EU" in directive_ids  # RoHS
    assert "EU 2023/1230" in directive_ids  # Machinery

    assert "EN 61439-1:2021" in doc["harmonized_standards"]
    assert "EN 60204-1:2018" in doc["harmonized_standards"]
    assert "EN ISO 13849-1:2023" in doc["harmonized_standards"]

    assert "content_hash" in doc
    assert len(doc["content_hash"]) == 64
    assert "signature" in doc
    assert len(doc["signature"]) > 0


def test_generate_eu_declaration_fails_closed_on_missing_mandatory() -> None:
    """Missing manufacturer or signatory fails closed with ValueError."""
    # Missing manufacturer
    with pytest.raises(ValueError, match="manufacturer_name"):
        generate_eu_declaration_of_conformity(
            {
                "signatory_name": "Erik",
            }
        )

    # Missing signatory
    with pytest.raises(ValueError, match="signatory name"):
        generate_eu_declaration_of_conformity(
            {
                "manufacturer_name": "Steps Systems",
            }
        )
