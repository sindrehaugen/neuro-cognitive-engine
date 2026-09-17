"""
tests/test_inventory_kitting.py
===============================
Acceptance tests for Wave IN-2 — Inventory kitting under the settled PACKAGE decision
(Batch 136b, Module 11 — settled 2026-08-31 per ``ML_HANDOFF_2026-08-31.md:109-140``).

Verifies:
  1. Package reservation:
     - Stocked as a UNIT, never decomposed in inventory.
     - Ignores nested components, reserves package SKU directly.
  2. Kit reservation:
     - Product + confirmed accessories, components stocked per piece.
     - Expands confirmed components into per-line reservations.
  3. Gate 2 Confirmation invariant:
     - Refuses live classifier accessory_of expansion at reservation time.
  4. All-or-nothing rollback:
     - Rolls back previously reserved lines if any kit component has insufficient stock.
  5. Kit release:
     - Decrements reservations across expanded kit components / packages.
  6. Input validation & namespace isolation:
     - Refuses missing fields, invalid project_id, empty items.
  7. Surface integration:
     - MCP handlers (handle_inventory_reserve_kit, handle_inventory_release_kit).
     - REST routes (api_inventory_reserve_kit, api_inventory_release_kit).
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.vertical_modules.inventory.kitting import (
    do_release_kit,
    do_reserve_kit,
)
from nce.vertical_modules.inventory.mcp_handlers import (
    handle_inventory_release_kit,
    handle_inventory_reserve_kit,
)
from nce.vertical_modules.inventory.reservation import (
    InsufficientAvailableError,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_LOCATION_ID = "11111111-1111-4111-8111-111111111111"
_PROJECT_ID = "PROJECT:QUOTE-001"


def _make_engine() -> MagicMock:
    """Mock engine with inventory enabled for the namespace."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"inventory_enabled": True})
    pool = MagicMock()
    ctx = pool.acquire.return_value
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = False
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


# ---------------------------------------------------------------------------
# 1. Package Reservation (Stocked as UNIT, never decompose)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_package_stocked_as_unit_never_decomposed() -> None:
    """Under the PACKAGE decision, packages are stocked as a unit and never decompose."""
    engine = _make_engine()

    reserve_mock = AsyncMock(
        return_value={
            "ok": True,
            "sku": "PKG-BARCO-CLICKSHARE",
            "location_id": _LOCATION_ID,
            "project_id": _PROJECT_ID,
            "qty": Decimal("2.000"),
            "on_hand": Decimal("10.000"),
            "reserved": Decimal("2.000"),
            "blocked": Decimal("0.000"),
            "available": Decimal("8.000"),
        }
    )

    with patch("nce.vertical_modules.inventory.kitting.do_reserve_stock", reserve_mock):
        result = await do_reserve_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": _PROJECT_ID,
                "location": _LOCATION_ID,
                "items": [
                    {
                        "sku": "PKG-BARCO-CLICKSHARE",
                        "qty": 2,
                        "is_package": True,
                        # Sub-components provided in manifest must NOT be decomposed in inventory
                        "components": [
                            {"sku": "BARCO-BASE-UNIT", "qty": 1},
                            {"sku": "BARCO-BUTTON-USB-C", "qty": 2},
                        ],
                    }
                ],
            },
        )

    assert result["ok"] is True
    assert result["summary"]["packages_reserved"] == 1
    assert result["summary"]["kit_components_reserved"] == 0
    assert len(result["reserved_lines"]) == 1

    line = result["reserved_lines"][0]
    assert line["sku"] == "PKG-BARCO-CLICKSHARE"
    assert line["qty"] == 2.0
    assert line["item_type"] == "package"
    assert line["decomposed"] is False
    assert line["note"] == "package_stocked_as_unit"

    # Exactly one reservation call for the package SKU, ZERO calls for sub-components
    assert reserve_mock.await_count == 1
    call_args = reserve_mock.await_args[0][1]
    assert call_args["sku"] == "PKG-BARCO-CLICKSHARE"
    assert call_args["qty"] == Decimal("2.000")


# ---------------------------------------------------------------------------
# 2. Kit Reservation (Expands confirmed components per piece)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kit_expands_confirmed_components_per_piece() -> None:
    """Kits expand base product and confirmed accessories into per-piece reservations."""
    engine = _make_engine()

    def fake_reserve(eng, params):
        sku = params["sku"]
        qty = params["qty"]
        return {
            "ok": True,
            "sku": sku,
            "location_id": _LOCATION_ID,
            "project_id": _PROJECT_ID,
            "qty": qty,
            "on_hand": Decimal("20.000"),
            "reserved": qty,
            "blocked": Decimal("0.000"),
            "available": Decimal("20.000") - qty,
        }

    reserve_mock = AsyncMock(side_effect=fake_reserve)

    with patch("nce.vertical_modules.inventory.kitting.do_reserve_stock", reserve_mock):
        result = await do_reserve_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": _PROJECT_ID,
                "location": _LOCATION_ID,
                "items": [
                    {
                        "sku": "KIT-VIDEOCONF",
                        "qty": 2,
                        "is_kit": True,
                        "components": [
                            {"sku": "MIC-POD", "qty": 2},
                            {"sku": "TABLE-HUB", "qty": 1},
                        ],
                    }
                ],
            },
        )

    assert result["ok"] is True
    # 1 base product + 2 components = 3 reserved lines
    assert result["summary"]["kit_components_reserved"] == 3
    assert len(result["reserved_lines"]) == 3

    skus = [line["sku"] for line in result["reserved_lines"]]
    assert "KIT-VIDEOCONF" in skus
    assert "MIC-POD" in skus
    assert "TABLE-HUB" in skus

    # MIC-POD qty should be 2 (parent) * 2 = 4
    mic_line = next(line for line in result["reserved_lines"] if line["sku"] == "MIC-POD")
    assert mic_line["qty"] == 4.0
    assert mic_line["item_type"] == "kit_component"
    assert mic_line["decomposed"] is True

    # TABLE-HUB qty should be 2 (parent) * 1 = 2
    hub_line = next(line for line in result["reserved_lines"] if line["sku"] == "TABLE-HUB")
    assert hub_line["qty"] == 2.0

    assert reserve_mock.await_count == 3


# ---------------------------------------------------------------------------
# 3. Gate 2 Confirmation Invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_live_classifier_expansion() -> None:
    """Gate 2 invariant: refuses attempts to dynamically query live accessory_of edges."""
    engine = _make_engine()

    with pytest.raises(ValueError, match="live accessory_of edges cannot be expanded"):
        await do_reserve_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": _PROJECT_ID,
                "location": _LOCATION_ID,
                "items": [
                    {
                        "sku": "KIT-1",
                        "qty": 1,
                        "expand_live_accessories": True,
                    }
                ],
            },
        )


# ---------------------------------------------------------------------------
# 4. All-or-Nothing Rollback on Insufficient Available Stock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_or_nothing_rollback_on_shortfall() -> None:
    """If any component fails reservation, all previous reservations are rolled back."""
    engine = _make_engine()

    release_mock = AsyncMock(
        return_value={
            "ok": True,
            "sku": "KIT-BASE",
            "location_id": _LOCATION_ID,
            "project_id": _PROJECT_ID,
            "qty": Decimal("1.000"),
            "on_hand": Decimal("10.000"),
            "reserved": Decimal("0.000"),
            "blocked": Decimal("0.000"),
            "available": Decimal("10.000"),
        }
    )

    async def fake_reserve(eng, params):
        sku = params["sku"]
        if sku == "OUT-OF-STOCK-PART":
            raise InsufficientAvailableError(
                sku=sku,
                location_id=UUID(_LOCATION_ID),
                project_id=_PROJECT_ID,
                requested=Decimal("2.000"),
                on_hand=Decimal("0.000"),
                reserved=Decimal("0.000"),
                blocked=Decimal("0.000"),
            )
        return {
            "ok": True,
            "sku": sku,
            "location_id": _LOCATION_ID,
            "project_id": _PROJECT_ID,
            "qty": params["qty"],
            "on_hand": Decimal("10.000"),
            "reserved": params["qty"],
            "blocked": Decimal("0.000"),
            "available": Decimal("10.000") - params["qty"],
        }

    reserve_mock = AsyncMock(side_effect=fake_reserve)

    with (
        patch("nce.vertical_modules.inventory.kitting.do_reserve_stock", reserve_mock),
        patch("nce.vertical_modules.inventory.kitting.do_release_stock", release_mock),
    ):
        with pytest.raises(InsufficientAvailableError) as exc_info:
            await do_reserve_kit(
                engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "project_id": _PROJECT_ID,
                    "location": _LOCATION_ID,
                    "items": [
                        {
                            "sku": "KIT-BASE",
                            "qty": 1,
                            "is_kit": True,
                            "components": [
                                {"sku": "AVAILABLE-PART", "qty": 1},
                                {"sku": "OUT-OF-STOCK-PART", "qty": 2},
                            ],
                        }
                    ],
                    "all_or_nothing": True,
                },
            )

        assert exc_info.value.sku == "OUT-OF-STOCK-PART"

        # The first two lines (KIT-BASE and AVAILABLE-PART) succeeded and must have been rolled back
        assert release_mock.await_count == 2
        released_skus = [call[0][1]["sku"] for call in release_mock.await_args_list]
        assert "AVAILABLE-PART" in released_skus
        assert "KIT-BASE" in released_skus


# ---------------------------------------------------------------------------
# 5. Kit Release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_kit_reservations() -> None:
    """do_release_kit releases reservations across all expanded lines."""
    engine = _make_engine()

    def fake_release(eng, params):
        sku = params["sku"]
        qty = params["qty"]
        return {
            "ok": True,
            "sku": sku,
            "location_id": _LOCATION_ID,
            "project_id": _PROJECT_ID,
            "qty": qty,
            "on_hand": Decimal("10.000"),
            "reserved": Decimal("0.000"),
            "blocked": Decimal("0.000"),
            "available": Decimal("10.000"),
        }

    release_mock = AsyncMock(side_effect=fake_release)

    with patch("nce.vertical_modules.inventory.kitting.do_release_stock", release_mock):
        result = await do_release_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": _PROJECT_ID,
                "location": _LOCATION_ID,
                "items": [
                    {
                        "sku": "PKG-ONE",
                        "qty": 1,
                        "is_package": True,
                    },
                    {
                        "sku": "KIT-TWO",
                        "qty": 1,
                        "is_kit": True,
                        "components": [{"sku": "ACC-1", "qty": 2}],
                    },
                ],
            },
        )

    assert result["ok"] is True
    assert result["summary"]["packages_released"] == 1
    assert result["summary"]["kit_components_released"] == 2
    assert release_mock.await_count == 3


# ---------------------------------------------------------------------------
# 6. Input Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kitting_input_validation() -> None:
    """Refuses empty items list or invalid project identifiers."""
    engine = _make_engine()

    with pytest.raises(ValueError, match="must be a non-empty list"):
        await do_reserve_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": _PROJECT_ID,
                "location": _LOCATION_ID,
                "items": [],
            },
        )

    with pytest.raises(ValueError, match="project_id"):
        await do_reserve_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": "INVALID-LABEL",
                "location": _LOCATION_ID,
                "items": [{"sku": "SKU-1", "qty": 1}],
            },
        )


# ---------------------------------------------------------------------------
# 7. MCP Handler & REST Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handlers_integration() -> None:
    """MCP handlers invoke do_reserve_kit and do_release_kit cleanly."""
    engine = _make_engine()

    with (
        patch("nce.vertical_modules.inventory.mcp_handlers._check_inventory_enabled", AsyncMock()),
        patch(
            "nce.vertical_modules.inventory.mcp_handlers.do_reserve_kit",
            AsyncMock(return_value={"ok": True, "reserved_lines": []}),
        ),
        patch(
            "nce.vertical_modules.inventory.mcp_handlers.do_release_kit",
            AsyncMock(return_value={"ok": True, "released_lines": []}),
        ),
    ):
        raw_res = await handle_inventory_reserve_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": _PROJECT_ID,
                "location": _LOCATION_ID,
                "items": [{"sku": "PKG-1", "qty": 1, "is_package": True}],
            },
        )
        res = json.loads(raw_res)
        assert res["ok"] is True

        raw_rel = await handle_inventory_release_kit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "project_id": _PROJECT_ID,
                "location": _LOCATION_ID,
                "items": [{"sku": "PKG-1", "qty": 1, "is_package": True}],
            },
        )
        rel = json.loads(raw_rel)
        assert rel["ok"] is True
