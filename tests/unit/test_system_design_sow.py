"""
tests/unit/test_system_design_sow.py
=====================================
Plain unit tests for Batch 060 — Module 6.Wave 5 (sow-adapter).

Covers:
  1. ``generate_sow`` is 0-DB (no DB imports or calls).
  2. ``generate_sow`` is deterministic — same input → identical output.
  3. ``generate_sow`` assembles per-room deliverables correctly.
  4. ``_assemble_sow_input`` builds per-room BOM inputs from graph data.
  5. ``do_generate_sow`` calls the pure transform with graph-mocked data.
  6. Freeze-on-issue: same design version → identical SoW + same version.
  7. Bumped design version (different updated_at) → new version number.

All tests are pure unit tests — DB reads are mocked (no asyncpg / Postgres).
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NS = "00000000-0000-4000-8000-000000000099"
_DESIGN_ID = "D-001"
_DESIGN_LABEL = "DESIGN:D-001"

_MINIMAL_SOW_INPUT: dict[str, Any] = {
    "project": {
        "id": "D-001",
        "name": "Test Project",
        "customerId": None,
        "customerName": "Acme AS",
        "contractValue": 500_000.0,
        "startDate": "2026-01-01",
        "endDate": "2026-06-30",
        "pm": "Sindre",
        "tier": 3,
    },
    "bomLines": [
        {
            "id": "DL-1",
            "category": "equipment",
            "description": "Samsung 75-tommer display",
            "qty": 2,
            "sellPrice": 25_000.0,
            "roomId": "FL:ACME:OSLO:BYGGA:F1:BOARDROOM",
            "roomName": "BOARDROOM",
            "ownership": "BETA",
        },
        {
            "id": "DL-2",
            "category": "equipment",
            "description": "Neat Bar Pro",
            "qty": 1,
            "sellPrice": 18_000.0,
            "roomId": "FL:ACME:OSLO:BYGGA:F1:BOARDROOM",
            "roomName": "BOARDROOM",
            "ownership": "BETA",
        },
        {
            "id": "DL-3",
            "category": "equipment",
            "description": "Apple TV 4K",
            "qty": 1,
            "sellPrice": 3_000.0,
            "roomId": "FL:ACME:OSLO:BYGGA:F2:LOUNGE",
            "roomName": "LOUNGE",
            "ownership": "BETA",
        },
    ],
    "rooms": [
        {"id": "FL:ACME:OSLO:BYGGA:F1:BOARDROOM", "name": "BOARDROOM", "type": "conference"},
        {"id": "FL:ACME:OSLO:BYGGA:F2:LOUNGE", "name": "LOUNGE", "type": "lounge"},
    ],
    "labor": [
        {
            "category": "installation",
            "externalHoursEst": 8.0,
            "internalHoursEst": 4.0,
            "rateCardSell": 1_200.0,
        },
        {
            "category": "programming",
            "externalHoursEst": 4.0,
            "internalHoursEst": 2.0,
            "rateCardSell": 1_500.0,
        },
    ],
    "milestones": [
        {"name": "Kick-off", "plannedDate": "2026-01-15", "isMilestone": True, "completed": False},
        {"name": "Levering", "plannedDate": "2026-03-01", "isMilestone": True, "completed": False},
        {"name": "Intern oppgave", "plannedDate": None, "isMilestone": False, "completed": False},
    ],
    "serviceContracts": [
        {
            "id": "SC-1",
            "name": "Example Gold",
            "tier": "COMPLETE",
            "coverageLevel": "FULL",
            "responseSpeed": "4H",
            "monthlyTotal": 5_000.0,
            "roomId": None,
        }
    ],
    "invoiceSchedule": {
        "profile": "example_standard",
        "paymentTermsDays": 30,
        "hwSigningPct": 100,
        "hwDeliveryPct": 0,
        "hwInstallPct": 0,
        "hwHandoverPct": 0,
        "softSigningPct": 50,
        "softDeliveryPct": 0,
        "softInstallPct": 0,
        "softHandoverPct": 50,
        "softMonthlyPct": 0,
    },
    "communications": {
        "count": 3,
        "decisions": ["Samsung 75\" valgt"],
        "actions": ["Tilbud sendes uke 3"],
        "products": ["Samsung", "Neat"],
        "questions": ["Ønsker de veggskinne?"],
    },
}


def _make_engine() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. generate_sow is a 0-DB pure function
# ---------------------------------------------------------------------------


def test_generate_sow_has_no_db_imports() -> None:
    """generate_sow must not import or touch asyncpg / scoped_pg_session."""
    import inspect

    from nce.vertical_modules.system_design import sow as sow_module

    src = inspect.getsource(sow_module.generate_sow)
    assert "asyncpg" not in src, "generate_sow must not reference asyncpg"
    assert "scoped_pg_session" not in src, "generate_sow must not call scoped_pg_session"
    assert "await" not in src, "generate_sow must not be async / use await"


# ---------------------------------------------------------------------------
# 2. generate_sow is deterministic
# ---------------------------------------------------------------------------


def test_generate_sow_is_deterministic() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    inp = copy.deepcopy(_MINIMAL_SOW_INPUT)
    doc1 = generate_sow(inp, version_number=7)
    doc2 = generate_sow(copy.deepcopy(inp), version_number=7)

    # Everything except generatedAt (timestamp) must be equal.
    doc1.pop("generatedAt")
    doc2.pop("generatedAt")
    assert doc1 == doc2


# ---------------------------------------------------------------------------
# 3. generate_sow assembles per-room deliverables
# ---------------------------------------------------------------------------


def test_generate_sow_groups_deliverables_per_room() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)

    room_names = {d["roomName"] for d in doc["deliverables"]}
    assert "BOARDROOM" in room_names
    assert "LOUNGE" in room_names

    boardroom = next(d for d in doc["deliverables"] if d["roomName"] == "BOARDROOM")
    assert boardroom["lineCount"] == 2
    assert boardroom["totalSell"] == pytest.approx(2 * 25_000 + 1 * 18_000)


def test_generate_sow_deliverables_sorted_by_total_sell_desc() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)
    totals = [d["totalSell"] for d in doc["deliverables"]]
    assert totals == sorted(totals, reverse=True)


def test_generate_sow_version_and_doc_ref() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=42)
    assert doc["versionNumber"] == 42
    assert doc["documentRef"] == "D-001-v42"


def test_generate_sow_labor_aggregation() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)
    # installation: (8+4)*1200 = 14400; programming: (4+2)*1500 = 9000
    assert doc["laborTotalHours"] == pytest.approx(18.0)
    assert doc["laborTotalSell"] == pytest.approx(14_400 + 9_000)


def test_generate_sow_milestones_only_milestone_flag() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)
    # "Intern oppgave" has isMilestone=False → excluded
    assert len(doc["timeline"]) == 2
    names = {m["name"] for m in doc["timeline"]}
    assert "Intern oppgave" not in names


def test_generate_sow_managed_services() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)
    assert len(doc["managedServices"]) == 1
    svc = doc["managedServices"][0]
    assert svc["tier"] == "Gold — fullstendig"
    assert svc["monthlyPrice"] == pytest.approx(5_000.0)
    assert svc["annualValue"] == pytest.approx(60_000.0)


def test_generate_sow_invoicing_hw_only() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)
    assert doc["invoicing"] is not None
    assert doc["invoicing"]["paymentTermsDays"] == 30
    # hwSigningPct=100 only row
    assert len(doc["invoicing"]["hwBreakdown"]) == 1
    assert doc["invoicing"]["hwBreakdown"][0]["trigger"] == "Signering"


def test_generate_sow_captured_intelligence() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)
    ci = doc["capturedIntelligence"]
    assert ci is not None
    assert ci["interactionCount"] == 3
    assert "Samsung 75\" valgt" in ci["decisions"]


def test_generate_sow_no_communications_returns_none() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    inp = copy.deepcopy(_MINIMAL_SOW_INPUT)
    inp["communications"] = None
    doc = generate_sow(inp, version_number=1)
    assert doc["capturedIntelligence"] is None


def test_generate_sow_acceptance_and_terms_present() -> None:
    from nce.vertical_modules.system_design.sow import generate_sow

    doc = generate_sow(copy.deepcopy(_MINIMAL_SOW_INPUT), version_number=1)
    assert len(doc["acceptance"]) >= 4
    assert len(doc["terms"]) >= 5


# ---------------------------------------------------------------------------
# 4. _assemble_sow_input: per-room BOM assembly from graph data
# ---------------------------------------------------------------------------


def test_assemble_sow_input_rooms_identified_by_depth() -> None:
    """FLs with exactly 5 colons (depth 6) are treated as ROOMs."""
    from nce.vertical_modules.system_design.sow import _assemble_sow_input

    fl_nodes = [
        {"label": "FL:ACME:OSLO:BYGGA:F1:BOARDROOM"},  # ROOM — 5 colons
        {"label": "FL:ACME:OSLO:BYGGA:F1"},  # FLOOR — 4 colons, NOT a room
        {"label": "FL:ACME:OSLO:BYGGA"},  # BUILDING
    ]
    fl_to_lines: dict[str, list[dict[str, Any]]] = {
        "FL:ACME:OSLO:BYGGA:F1:BOARDROOM": [{"label": "DESIGN_LINE:D-001:L1"}],
        "FL:ACME:OSLO:BYGGA:F1": [],
        "FL:ACME:OSLO:BYGGA": [],
    }
    design_lines = [{"label": "DESIGN_LINE:D-001:L1"}]
    design_meta = {"updated_at": "2026-06-01T10:00:00Z"}

    sow_input = _assemble_sow_input(
        design_label=_DESIGN_LABEL,
        design_meta=design_meta,
        design_lines=design_lines,
        fl_nodes=fl_nodes,
        fl_to_lines=fl_to_lines,
    )

    room_ids = {r["id"] for r in sow_input["rooms"]}
    assert "FL:ACME:OSLO:BYGGA:F1:BOARDROOM" in room_ids
    # non-ROOM FLs must NOT appear in rooms list
    assert "FL:ACME:OSLO:BYGGA:F1" not in room_ids
    assert "FL:ACME:OSLO:BYGGA" not in room_ids


def test_assemble_sow_input_bom_lines_room_assigned() -> None:
    """BOM lines are assigned to the FL room that has a 'needs' edge to them."""
    from nce.vertical_modules.system_design.sow import _assemble_sow_input

    fl_nodes = [{"label": "FL:ACME:OSLO:BYGGA:F1:BOARDROOM"}]
    fl_to_lines: dict[str, list[dict[str, Any]]] = {
        "FL:ACME:OSLO:BYGGA:F1:BOARDROOM": [
            {"label": "DESIGN_LINE:D-001:L1"},
            {"label": "DESIGN_LINE:D-001:L2"},
        ],
    }
    design_lines = [
        {"label": "DESIGN_LINE:D-001:L1"},
        {"label": "DESIGN_LINE:D-001:L2"},
    ]
    design_meta = {"updated_at": "2026-06-01T10:00:00Z"}

    sow_input = _assemble_sow_input(
        design_label=_DESIGN_LABEL,
        design_meta=design_meta,
        design_lines=design_lines,
        fl_nodes=fl_nodes,
        fl_to_lines=fl_to_lines,
    )

    assert len(sow_input["bomLines"]) == 2
    for line in sow_input["bomLines"]:
        assert line["roomId"] == "FL:ACME:OSLO:BYGGA:F1:BOARDROOM"


def test_assemble_sow_input_unassigned_lines_have_no_room() -> None:
    """DESIGN_LINE nodes with no FL 'needs' edge get roomId=None."""
    from nce.vertical_modules.system_design.sow import _assemble_sow_input

    fl_nodes: list[dict[str, Any]] = []
    fl_to_lines: dict[str, list[dict[str, Any]]] = {}
    design_lines = [{"label": "DESIGN_LINE:D-001:L99"}]
    design_meta = {"updated_at": "2026-06-01T10:00:00Z"}

    sow_input = _assemble_sow_input(
        design_label=_DESIGN_LABEL,
        design_meta=design_meta,
        design_lines=design_lines,
        fl_nodes=fl_nodes,
        fl_to_lines=fl_to_lines,
    )

    assert sow_input["bomLines"][0]["roomId"] is None


# ---------------------------------------------------------------------------
# 5. do_generate_sow — mocked graph reads, calls pure transform
# ---------------------------------------------------------------------------


def _make_scoped_session_mock(
    design_meta: dict[str, Any],
    design_lines: list[dict[str, Any]],
    fl_nodes: list[dict[str, Any]],
    fl_to_lines: dict[str, list[dict[str, Any]]],
) -> Any:
    """Build an asynccontextmanager mock that returns a connection mock."""
    from contextlib import asynccontextmanager

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=design_meta if design_meta else None)

    # fetch is called for: design_lines, fl_nodes, then N * fl_design_lines
    fetch_side_effects: list[list[dict[str, Any]]] = [design_lines, fl_nodes]
    for fl in fl_nodes:
        fetch_side_effects.append(fl_to_lines.get(fl["label"], []))
    conn.fetch = AsyncMock(side_effect=fetch_side_effects)

    @asynccontextmanager  # type: ignore[misc]
    async def _mock_scoped_session(pool: Any, ns_id: Any) -> Any:
        yield conn

    return _mock_scoped_session


@pytest.mark.asyncio
async def test_do_generate_sow_returns_sow_doc() -> None:
    from nce.vertical_modules.system_design.sow import do_generate_sow

    design_meta = {"label": _DESIGN_LABEL, "updated_at": "2026-06-01T10:00:00Z"}
    design_lines = [{"label": "DESIGN_LINE:D-001:L1"}]
    fl_nodes = [{"label": "FL:ACME:OSLO:BYGGA:F1:BOARDROOM"}]
    fl_to_lines: dict[str, list[dict[str, Any]]] = {
        "FL:ACME:OSLO:BYGGA:F1:BOARDROOM": [{"label": "DESIGN_LINE:D-001:L1"}],
    }

    engine = _make_engine()
    mock_session = _make_scoped_session_mock(design_meta, design_lines, fl_nodes, fl_to_lines)

    with patch("nce.vertical_modules.system_design.sow.scoped_pg_session", mock_session):
        result = await do_generate_sow(engine, {"namespace_id": _NS, "design_id": _DESIGN_ID})

    assert "sow" in result
    sow = result["sow"]
    assert sow["versionNumber"] == result["version_number"]
    assert "documentRef" in sow
    assert "deliverables" in sow


@pytest.mark.asyncio
async def test_do_generate_sow_missing_namespace_id_raises() -> None:
    from nce.vertical_modules.system_design.sow import do_generate_sow

    engine = _make_engine()
    with pytest.raises(ValueError, match="namespace_id"):
        await do_generate_sow(engine, {"design_id": _DESIGN_ID})


@pytest.mark.asyncio
async def test_do_generate_sow_missing_design_id_raises() -> None:
    from nce.vertical_modules.system_design.sow import do_generate_sow

    engine = _make_engine()
    with pytest.raises(ValueError, match="design_id"):
        await do_generate_sow(engine, {"namespace_id": _NS})


@pytest.mark.asyncio
async def test_do_generate_sow_not_found_raises() -> None:
    """A missing DESIGN node (fetchrow returns None) raises ValueError."""
    from nce.vertical_modules.system_design.sow import do_generate_sow

    engine = _make_engine()

    # fetchrow returns None → design not found
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    from contextlib import asynccontextmanager

    @asynccontextmanager  # type: ignore[misc]
    async def _empty_session(pool: Any, ns_id: Any) -> Any:
        yield conn

    with patch("nce.vertical_modules.system_design.sow.scoped_pg_session", _empty_session):
        with pytest.raises(ValueError, match="DESIGN node not found"):
            await do_generate_sow(engine, {"namespace_id": _NS, "design_id": "NONEXISTENT"})


# ---------------------------------------------------------------------------
# 6. Freeze-on-issue: same design version → same version_number + same doc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freeze_on_issue_same_design_version_same_output() -> None:
    """Two calls with the same design state must yield the same version_number."""
    from nce.vertical_modules.system_design.sow import do_generate_sow

    design_meta = {"label": _DESIGN_LABEL, "updated_at": "2026-06-01T10:00:00Z"}
    design_lines: list[dict[str, Any]] = []
    fl_nodes: list[dict[str, Any]] = []
    fl_to_lines: dict[str, list[dict[str, Any]]] = {}

    engine = _make_engine()

    async def _run() -> dict[str, Any]:
        mock_session = _make_scoped_session_mock(design_meta, design_lines, fl_nodes, fl_to_lines)
        with patch("nce.vertical_modules.system_design.sow.scoped_pg_session", mock_session):
            return await do_generate_sow(engine, {"namespace_id": _NS, "design_id": _DESIGN_ID})

    r1 = await _run()
    r2 = await _run()

    # Same design state → same version_number
    assert r1["version_number"] == r2["version_number"]
    # documentRef is stable (only generatedAt differs)
    assert r1["sow"]["documentRef"] == r2["sow"]["documentRef"]
    assert r1["sow"]["versionNumber"] == r2["sow"]["versionNumber"]


# ---------------------------------------------------------------------------
# 7. Bumped design version → new version number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bumped_design_version_yields_new_version_number() -> None:
    """A different updated_at (= design was mutated) must yield a new version_number."""
    from nce.vertical_modules.system_design.sow import do_generate_sow

    design_meta_v1 = {"label": _DESIGN_LABEL, "updated_at": "2026-06-01T10:00:00Z"}
    design_meta_v2 = {"label": _DESIGN_LABEL, "updated_at": "2026-06-15T14:30:00Z"}
    design_lines: list[dict[str, Any]] = []
    fl_nodes: list[dict[str, Any]] = []
    fl_to_lines: dict[str, list[dict[str, Any]]] = {}

    engine = _make_engine()

    async def _run(meta: dict[str, Any]) -> dict[str, Any]:
        mock_session = _make_scoped_session_mock(meta, design_lines, fl_nodes, fl_to_lines)
        with patch("nce.vertical_modules.system_design.sow.scoped_pg_session", mock_session):
            return await do_generate_sow(engine, {"namespace_id": _NS, "design_id": _DESIGN_ID})

    r_v1 = await _run(design_meta_v1)
    r_v2 = await _run(design_meta_v2)

    assert r_v1["version_number"] != r_v2["version_number"], (
        "Different design states must yield different version numbers "
        f"(got {r_v1['version_number']} for both)"
    )


# ---------------------------------------------------------------------------
# 8. Caller-supplied version_number is honoured (frozen flag)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caller_supplied_version_is_used() -> None:
    from nce.vertical_modules.system_design.sow import do_generate_sow

    design_meta = {"label": _DESIGN_LABEL, "updated_at": "2026-06-01T10:00:00Z"}
    design_lines: list[dict[str, Any]] = []
    fl_nodes: list[dict[str, Any]] = []
    fl_to_lines: dict[str, list[dict[str, Any]]] = {}

    engine = _make_engine()
    mock_session = _make_scoped_session_mock(design_meta, design_lines, fl_nodes, fl_to_lines)

    with patch("nce.vertical_modules.system_design.sow.scoped_pg_session", mock_session):
        result = await do_generate_sow(
            engine,
            {"namespace_id": _NS, "design_id": _DESIGN_ID, "version_number": 77},
        )

    assert result["version_number"] == 77
    assert result["frozen"] is True
    assert result["sow"]["versionNumber"] == 77
    # documentRef uses the clean design id (the internal "DESIGN:" graph-label
    # prefix is deliberately stripped in _build_sow_input — it must not leak
    # into a customer-facing SoW deliverable). Matches the pure-transform
    # contract "<project.id>-v<version>".
    assert result["sow"]["documentRef"] == "D-001-v77"
