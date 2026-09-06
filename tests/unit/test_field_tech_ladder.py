"""tests/unit/test_field_tech_ladder.py
======================================
Unit tests for Wave FT-1:
  - BOM_LINE status progression (INSTALLED, TESTED)
  - Contract-A ownership registration for field_tech transitions
  - FUNCTIONAL_LOCATION intent -> as-built promotion on install
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from nce.bom_lines import StatusState
from nce.vertical_modules.field_tech.checklist import do_complete_checklist
from nce.vertical_modules.field_tech.scan import do_scan_serial

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OWNERSHIP_JSON = _REPO_ROOT / "nce" / "config_data" / "node-ownership.json"

_NS = "11111111-1111-4111-8111-111111111111"
_WO_ID = "WO-UNIT-001"


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
            yield pool

    monkeypatch.setattr("nce.vertical_modules.field_tech.scan.scoped_pg_session", _fake_scoped)
    monkeypatch.setattr("nce.vertical_modules.field_tech.checklist.scoped_pg_session", _fake_scoped)
    mock_assert_owner = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.scan.assert_owner", mock_assert_owner, raising=False
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.checklist.assert_owner", mock_assert_owner, raising=False
    )


def test_status_state_literal_includes_tested() -> None:
    """StatusState Literal in nce/bom_lines.py must contain TESTED."""
    args = get_args(StatusState)
    assert "ORDERED" in args
    assert "DELIVERED" in args
    assert "INSTALLED" in args
    assert "TESTED" in args


def test_node_ownership_contains_field_tech_transitions() -> None:
    """node-ownership.json must declare field_tech ownership for status:installed and status:tested."""
    raw = json.loads(_OWNERSHIP_JSON.read_text(encoding="utf-8"))
    entries = raw.get("ownership", [])
    fl_entries = [
        e
        for e in entries
        if e.get("node_type") == "BOM_LINE" and e.get("owner_engine") == "field_tech"
    ]
    transitions = {e.get("transition") for e in fl_entries}
    assert "status:installed" in transitions
    assert "status:tested" in transitions


def _mock_bom_line_row(status: str) -> dict[str, Any]:
    return {
        "id": UUID("44444444-4444-4444-8444-444444444444"),
        "namespace_id": UUID(_NS),
        "bom_line_label": "BOM_LINE:Q-001:L-1",
        "quote_id": "Q-001",
        "line_ref": "L-1",
        "qty": 1.0,
        "unit_price": 100.0,
        "line_total": 100.0,
        "priced": True,
        "currency": "NOK",
        "origin_kind": "manual",
        "origin_ref": None,
        "writer_engine": "field_tech",
        "status": status,
        "status_changed_at": None,
        "frozen_at": None,
    }


@pytest.mark.asyncio
async def test_do_scan_serial_advances_installed_and_promotes_location() -> None:
    """do_scan_serial advances BOM_LINE status to INSTALLED and writes promoted_to_asbuilt edges."""
    conn = AsyncMock()
    # Mock wo lookup: return work order with location_id
    conn.fetchrow.side_effect = [
        {"id": UUID("22222222-2222-4222-8222-222222222222"), "location_id": "SITE:BLD1:RM101"},
        # bom_lines update_bom_line_status lookup / update
        {"owner_engine": "field_tech"},  # assert_owner
        _mock_bom_line_row("INSTALLED"),  # update RETURNING
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_scan_serial(
        pool,
        {
            "namespace_id": _NS,
            "work_order_id": _WO_ID,
            "bom_line_id": "BOM_LINE:Q-001:L-1",
            "serial": "SN-AMP-001",
        },
    )

    assert res["status"] == "scanned"
    assert res["serial"] == "SN-AMP-001"
    assert res.get("asbuilt_location") == "AsBuilt:FUNCTIONAL_LOCATION:SITE:BLD1:RM101"

    all_calls_str = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "installed_as" in all_calls_str
    assert "promoted_to_asbuilt" in all_calls_str
    assert "as_built_confirms" in all_calls_str
    assert "lives_in" in all_calls_str


@pytest.mark.asyncio
async def test_do_complete_checklist_advances_tested() -> None:
    """do_complete_checklist advances BOM_LINE status to TESTED on completed checklist."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"partner_scope_id": None},  # wo lookup
        # checklist insert returning
        {
            "id": UUID("33333333-3333-4333-8333-333333333333"),
            "checklist_id": "CL-001",
            "work_order_id": _WO_ID,
            "namespace_id": UUID(_NS),
            "partner_scope_id": None,
            "template_id": "install_standard",
            "items": [],
            "completed_at": None,
            "raw": {},
            "created_at": None,
            "updated_at": None,
        },
        # update_bom_line_status: assert_owner
        {"owner_engine": "field_tech"},
        # update RETURNING
        _mock_bom_line_row("TESTED"),
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_complete_checklist(
        pool,
        {
            "namespace_id": _NS,
            "work_order_id": _WO_ID,
            "checklist_id": "CL-001",
            "template_id": "install_standard",
            "items": [
                {"id": "pre_site_inspection", "required": True, "ticked": True},
                {"id": "mounting_hardware_secure", "required": True, "ticked": True},
                {"id": "cable_dressing_and_labels", "required": True, "ticked": True},
                {"id": "serial_number_scans", "required": True, "ticked": True},
                {"id": "firmware_update_and_config", "required": True, "ticked": True},
                {"id": "audio_video_commissioning", "required": True, "ticked": True},
                {"id": "site_cleanup_and_photo", "required": True, "ticked": True},
            ],
            "bom_line_id": "BOM_LINE:Q-001:L-1",
        },
    )

    assert res["is_complete"] is True
    assert "BOM_LINE:Q-001:L-1" in res.get("tested_lines", [])
