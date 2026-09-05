"""
tests/unit/test_inventory_surface.py
======================================
Acceptance tests for Batch 131 — Module 11.Wave 3 (stock-surface).

Covers:
  1. The ``inventory`` MCP-handler / admin-handler packages import cleanly.
  2. ``inventory_stock_levels`` / ``inventory_transfer_stock`` /
     ``inventory_record_consumption`` are registered in ``TOOL_REGISTRY``
     with the correct flags, per the tool table in
     ``docs/vertical_engines/11-inventory-engine.md``.
  3. Each ``handle_*`` returns the underlying Wave 2 core's result on a good
     payload, raises ``McpError(-32602)`` for a missing ``namespace_id``, and
     returns a structured ``{"error": ...}`` (never a crash) when the core
     raises ``InsufficientStockError``.
  4. Tool-count assertion reflects the +3 inventory tools (now 115), and
     since Batch 138a the +11 surface-completion tools (registry now 135).
  5. The three REST routes are mounted in the admin app and return the same
     shape as the cores, including the 409 mapping for
     ``InsufficientStockError`` and 503 when no engine is connected.
  6. A non-finite ``float`` in a core result is neutralised by
     ``_shared._json_safe`` instead of crashing Starlette's
     ``allow_nan=False`` encoder and being mis-filed as a 422.

The Wave 2 cores (``stock.do_stock_levels`` / ``do_transfer_stock`` /
``do_record_consumption``) are patched at the point of use in both surface
modules (mirrors ``test_economy_surface.py``'s ``_patch_loaders`` pattern),
so these are pure unit tests — no DB, no Redis, no real Postgres connection.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.vertical_modules.inventory.stock import InsufficientStockError

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_LOCATION_A_UUID = uuid4()
_LOCATION_B_UUID = uuid4()
_LOCATION_A = str(_LOCATION_A_UUID)
_LOCATION_B = str(_LOCATION_B_UUID)

_STOCK_LEVELS_RESULT: dict[str, Any] = {
    "ok": True,
    "items": [
        {
            "sku": "SKU-1",
            "location_id": _LOCATION_A,
            "on_hand": Decimal("10.000"),
            "reserved": Decimal("0.000"),
            "blocked": Decimal("0.000"),
            "available": Decimal("10.000"),
        }
    ],
}

_TRANSFER_RESULT: dict[str, Any] = {
    "ok": True,
    "sku": "SKU-1",
    "from_location": _LOCATION_A,
    "to_location": _LOCATION_B,
    "qty": Decimal("2.000"),
    "from_on_hand": Decimal("8.000"),
    "to_on_hand": Decimal("2.000"),
}

_CONSUMPTION_RESULT: dict[str, Any] = {
    "ok": True,
    "sku": "SKU-1",
    "location": _LOCATION_A,
    "qty": Decimal("1.000"),
    "on_hand": Decimal("9.000"),
    "work_order": "WO-42",
}


def _insufficient_stock_error() -> InsufficientStockError:
    return InsufficientStockError(
        sku="SKU-1",
        location_id=_LOCATION_A_UUID,
        requested=Decimal("5.000"),
        available_on_hand=Decimal("1.000"),
    )


def _make_engine() -> MagicMock:
    """An engine whose namespace HAS opted in to the Inventory vertical.

    Batch 140a put a deny-by-default ``metadata.inventory.enabled`` gate on
    every ``handle_inventory_*`` handler and every ``api_inventory_*`` route.
    A bare ``MagicMock()`` pool would refuse every call here, so the mock
    pool returns the opted-in row the guard reads. No assertion below is
    changed, weakened or removed -- only the fixture namespace opts in.
    """
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"inventory_enabled": True})
    pool = MagicMock()
    ctx = pool.acquire.return_value
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = False
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_request(
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    """Minimal Starlette-like request mock (mirrors test_project_routes.py)."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    return req


# ---------------------------------------------------------------------------
# 1. Package imports
# ---------------------------------------------------------------------------


def test_package_imports() -> None:
    import nce.admin_handlers.inventory  # noqa: F401
    import nce.vertical_modules.inventory.mcp_handlers  # noqa: F401


# ---------------------------------------------------------------------------
# 2. Tool registry — flags + count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,expected_flags",
    [
        (
            "inventory_stock_levels",
            {"cacheable": True, "admin_only": False, "mutation": False, "migration": False},
        ),
        (
            "inventory_transfer_stock",
            {"cacheable": False, "admin_only": True, "mutation": True, "migration": False},
        ),
        (
            "inventory_record_consumption",
            {"cacheable": False, "admin_only": True, "mutation": True, "migration": False},
        ),
    ],
)
def test_inventory_tools_registered_with_correct_flags(
    tool_name: str, expected_flags: dict[str, bool]
) -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert tool_name in TOOL_REGISTRY, f"{tool_name!r} not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY[tool_name]
    for flag, expected in expected_flags.items():
        actual = getattr(spec, flag)
        assert actual == expected, f"{tool_name}.{flag}: expected {expected!r}, got {actual!r}"


def test_tool_count_updated_for_inventory() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert "inventory_stock_levels" in TOOL_REGISTRY
    assert "inventory_transfer_stock" in TOOL_REGISTRY
    assert "inventory_record_consumption" in TOOL_REGISTRY
    assert len(TOOL_REGISTRY) == 174, (
        f"Expected 135 tools (112 + 3 inventory from Batch 131 + 1 assets_ping "
        f"from Batch 141 + 3 assets tools from Batch 143 + 1 system_design tool "
        f"from Batch 067b + 2 system_design authoring tools from Batch 067c "
        f"+ 1 system_design validator from Batch 067d "
        f"+ 1 system_design retire tool from Batch 067h "
        f"+ 11 inventory tools from Batch 138a, M11.W10a -- surface completion, "
        f"registering the Inventory cores Batch 131's single surface wave predated + 8 hr tools from Module 13 (HR engine)), "
        f"got {len(TOOL_REGISTRY)}: "
        f"{sorted(TOOL_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# 3. handle_* — MCP surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_stock_levels_returns_core_result() -> None:
    from nce.vertical_modules.inventory import mcp_handlers

    with patch.object(
        mcp_handlers, "do_stock_levels", AsyncMock(return_value=_STOCK_LEVELS_RESULT)
    ):
        result = await mcp_handlers.handle_inventory_stock_levels(
            _make_engine(), {"namespace_id": _NAMESPACE_ID, "sku": "SKU-1"}
        )
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["items"][0]["sku"] == "SKU-1"
    assert parsed["items"][0]["on_hand"] == "10.000"  # Decimal -> str via default=str


@pytest.mark.asyncio
async def test_handle_stock_levels_missing_namespace_id_raises_mcp_error() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.inventory.mcp_handlers import handle_inventory_stock_levels

    with pytest.raises(McpError) as exc_info:
        await handle_inventory_stock_levels(_make_engine(), {})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_transfer_stock_returns_core_result() -> None:
    from nce.vertical_modules.inventory import mcp_handlers

    with patch.object(mcp_handlers, "do_transfer_stock", AsyncMock(return_value=_TRANSFER_RESULT)):
        result = await mcp_handlers.handle_inventory_transfer_stock(
            _make_engine(),
            {
                "namespace_id": _NAMESPACE_ID,
                "sku": "SKU-1",
                "qty": 2,
                "from_location": _LOCATION_A,
                "to_location": _LOCATION_B,
            },
        )
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["from_on_hand"] == "8.000"


@pytest.mark.asyncio
async def test_handle_transfer_stock_missing_namespace_id_raises_mcp_error() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.inventory.mcp_handlers import handle_inventory_transfer_stock

    with pytest.raises(McpError) as exc_info:
        await handle_inventory_transfer_stock(_make_engine(), {})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_transfer_stock_insufficient_stock_returns_structured_error() -> None:
    from nce.vertical_modules.inventory import mcp_handlers

    with patch.object(
        mcp_handlers, "do_transfer_stock", AsyncMock(side_effect=_insufficient_stock_error())
    ):
        result = await mcp_handlers.handle_inventory_transfer_stock(
            _make_engine(),
            {
                "namespace_id": _NAMESPACE_ID,
                "sku": "SKU-1",
                "qty": 5,
                "from_location": _LOCATION_A,
                "to_location": _LOCATION_B,
            },
        )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "ok" not in parsed
    assert parsed["sku"] == "SKU-1"
    assert parsed["available_on_hand"] == "1.000"


@pytest.mark.asyncio
async def test_handle_record_consumption_returns_core_result() -> None:
    from nce.vertical_modules.inventory import mcp_handlers

    with patch.object(
        mcp_handlers, "do_record_consumption", AsyncMock(return_value=_CONSUMPTION_RESULT)
    ):
        result = await mcp_handlers.handle_inventory_record_consumption(
            _make_engine(),
            {"namespace_id": _NAMESPACE_ID, "sku": "SKU-1", "qty": 1, "location": _LOCATION_A},
        )
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["work_order"] == "WO-42"


@pytest.mark.asyncio
async def test_handle_record_consumption_missing_namespace_id_raises_mcp_error() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.inventory.mcp_handlers import handle_inventory_record_consumption

    with pytest.raises(McpError) as exc_info:
        await handle_inventory_record_consumption(_make_engine(), {})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_record_consumption_insufficient_stock_returns_structured_error() -> None:
    from nce.vertical_modules.inventory import mcp_handlers

    with patch.object(
        mcp_handlers, "do_record_consumption", AsyncMock(side_effect=_insufficient_stock_error())
    ):
        result = await mcp_handlers.handle_inventory_record_consumption(
            _make_engine(),
            {"namespace_id": _NAMESPACE_ID, "sku": "SKU-1", "qty": 5, "location": _LOCATION_A},
        )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "ok" not in parsed
    assert parsed["requested"] == "5.000"


# ---------------------------------------------------------------------------
# 4. REST routes — mounted + same shape as the cores
# ---------------------------------------------------------------------------


def test_inventory_routes_mounted_in_admin_app() -> None:
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/inventory/stock-levels" in paths
    assert "/api/inventory/transfer-stock" in paths
    assert "/api/inventory/record-consumption" in paths


@pytest.mark.asyncio
async def test_api_inventory_stock_levels_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(
            inventory_mod, "do_stock_levels", AsyncMock(return_value=_STOCK_LEVELS_RESULT)
        ):
            req = _make_request(query={"namespace_id": _NAMESPACE_ID, "sku": "SKU-1"})
            resp = await inventory_mod.api_inventory_stock_levels(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["items"][0]["on_hand"] == "10.000"


@pytest.mark.asyncio
async def test_api_inventory_transfer_stock_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(
            inventory_mod, "do_transfer_stock", AsyncMock(return_value=_TRANSFER_RESULT)
        ):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "sku": "SKU-1",
                    "qty": 2,
                    "from_location": _LOCATION_A,
                    "to_location": _LOCATION_B,
                }
            )
            resp = await inventory_mod.api_inventory_transfer_stock(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_api_inventory_transfer_stock_insufficient_stock_returns_409() -> None:
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(
            inventory_mod, "do_transfer_stock", AsyncMock(side_effect=_insufficient_stock_error())
        ):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "sku": "SKU-1",
                    "qty": 5,
                    "from_location": _LOCATION_A,
                    "to_location": _LOCATION_B,
                }
            )
            resp = await inventory_mod.api_inventory_transfer_stock(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 409
    assert body["available_on_hand"] == "1.000"


@pytest.mark.asyncio
async def test_api_inventory_record_consumption_returns_ok_shape() -> None:
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(
            inventory_mod, "do_record_consumption", AsyncMock(return_value=_CONSUMPTION_RESULT)
        ):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "sku": "SKU-1",
                    "qty": 1,
                    "location": _LOCATION_A,
                    "work_order": "WO-42",
                }
            )
            resp = await inventory_mod.api_inventory_record_consumption(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200
    assert body["work_order"] == "WO-42"


@pytest.mark.asyncio
async def test_api_inventory_record_consumption_insufficient_stock_returns_409() -> None:
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(
            inventory_mod,
            "do_record_consumption",
            AsyncMock(side_effect=_insufficient_stock_error()),
        ):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "sku": "SKU-1",
                    "qty": 5,
                    "location": _LOCATION_A,
                }
            )
            resp = await inventory_mod.api_inventory_record_consumption(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 409
    assert body["available_on_hand"] == "1.000"


@pytest.mark.asyncio
async def test_api_inventory_routes_missing_namespace_id_returns_422() -> None:
    from nce import admin_state
    from nce.admin_handlers.inventory import (
        api_inventory_record_consumption,
        api_inventory_stock_levels,
        api_inventory_transfer_stock,
    )

    with patch.object(admin_state, "engine", _make_engine()):
        resp = await api_inventory_stock_levels(_make_request(query={}))
        assert resp.status_code == 422

        resp = await api_inventory_transfer_stock(_make_request(body={}))
        assert resp.status_code == 422

        resp = await api_inventory_record_consumption(_make_request(body={}))
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_inventory_routes_no_engine_returns_503() -> None:
    from nce import admin_state
    from nce.admin_handlers.inventory import (
        api_inventory_record_consumption,
        api_inventory_stock_levels,
        api_inventory_transfer_stock,
    )

    with patch.object(admin_state, "engine", None):
        resp = await api_inventory_stock_levels(
            _make_request(query={"namespace_id": _NAMESPACE_ID})
        )
        assert resp.status_code == 503

        resp = await api_inventory_transfer_stock(
            _make_request(body={"namespace_id": _NAMESPACE_ID})
        )
        assert resp.status_code == 503

        resp = await api_inventory_record_consumption(
            _make_request(body={"namespace_id": _NAMESPACE_ID})
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 6. Non-finite float serialisation  (regression: inventory's `_json_safe`
#    dropped `_neutralise_non_finite`, so a nan/inf sailed through into
#    Starlette's `allow_nan=False` encoder)
#
# `JSONResponse(_json_safe(result))` is built INSIDE each route's
# `try:` block, whose `except (ValueError, KeyError, TypeError)` maps to 422.
# `JSONResponse.__init__` renders the body eagerly, so the encoder's
# `ValueError("Out of range float values are not JSON compliant")` is caught
# by that handler and reported to the caller as a malformed request -- the
# exact mis-filing `economy.py`'s `_neutralise_non_finite` was added to stop.
#
# The Wave 2 cores currently return `Decimal` for every quantity (and
# `default=str` already renders even `Decimal("NaN")` safely), so these tests
# inject the non-finite float via the patched core rather than through a route
# payload: they gate the serialisation boundary against drift, they are not a
# reproduction of a caller-reachable path on today's cores.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_inventory_stock_levels_non_finite_float_serialises_successfully() -> None:
    """A non-finite ``float`` reaching the read route must be neutralised to a JSON
    string and returned 200 -- never crash the response encoder and get mis-filed as
    a 422 domain-validation error.

    Goes RED if ``_json_safe`` stops neutralising non-finite floats (mirrors
    ``test_economy_surface.py``'s NaN regression tests).
    """
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    poisoned: dict[str, Any] = {
        "ok": True,
        "items": [
            {
                "sku": "SKU-1",
                "location_id": _LOCATION_A,
                "on_hand": Decimal("10.000"),
                "reserved": Decimal("0.000"),
                "blocked": Decimal("0.000"),
                "available": float("inf"),
            }
        ],
    }

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(inventory_mod, "do_stock_levels", AsyncMock(return_value=poisoned)):
            req = _make_request(query={"namespace_id": _NAMESPACE_ID, "sku": "SKU-1"})
            resp = await inventory_mod.api_inventory_stock_levels(req)

    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200, f"non-finite float was mis-filed as {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["items"][0]["available"] == "inf"
    assert body["items"][0]["on_hand"] == "10.000"


@pytest.mark.asyncio
async def test_api_inventory_record_consumption_non_finite_float_serialises_successfully() -> None:
    """Same guard on a write route: a non-finite ``float`` echoed into the
    consumption result serialises to a string at 200 rather than 422."""
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    poisoned: dict[str, Any] = {**_CONSUMPTION_RESULT, "on_hand": float("nan")}

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(inventory_mod, "do_record_consumption", AsyncMock(return_value=poisoned)):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "sku": "SKU-1",
                    "qty": 1,
                    "location": _LOCATION_A,
                }
            )
            resp = await inventory_mod.api_inventory_record_consumption(req)

    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 200, f"non-finite float was mis-filed as {resp.status_code}: {body}"
    assert body["on_hand"] == "nan"
    assert body["qty"] == "1.000"


@pytest.mark.asyncio
async def test_api_inventory_insufficient_stock_non_finite_float_serialises_successfully() -> None:
    """The 409 refusal body also goes through ``_json_safe``; a non-finite float
    there must not turn the structured 409 into a 422."""
    from nce import admin_state
    from nce.admin_handlers import inventory as inventory_mod

    exc = InsufficientStockError(
        sku="SKU-1",
        location_id=_LOCATION_A_UUID,
        requested=Decimal("5.000"),
        available_on_hand=float("-inf"),
    )

    with patch.object(admin_state, "engine", _make_engine()):
        with patch.object(inventory_mod, "do_record_consumption", AsyncMock(side_effect=exc)):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "sku": "SKU-1",
                    "qty": 5,
                    "location": _LOCATION_A,
                }
            )
            resp = await inventory_mod.api_inventory_record_consumption(req)

    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert resp.status_code == 409, (
        f"non-finite float turned the 409 refusal into {resp.status_code}: {body}"
    )
    assert body["available_on_hand"] == "-inf"


def test_inventory_and_economy_share_one_json_safe() -> None:
    """The drift that caused this bug was two separate ``_json_safe`` copies whose
    behaviour diverged while inventory's docstring claimed to mirror economy's.
    Assert there is now exactly ONE implementation behind both surfaces, so a
    future fix to one can never again silently skip the other."""
    from nce.admin_handlers import assets as assets_mod
    from nce.admin_handlers import economy as economy_mod
    from nce.admin_handlers import inventory as inventory_mod
    from nce.admin_handlers._shared import _json_safe, _require_namespace_id

    assert inventory_mod._json_safe is economy_mod._json_safe is _json_safe
    assert inventory_mod._require_namespace_id is _require_namespace_id
    assert assets_mod._require_namespace_id is _require_namespace_id


# ---------------------------------------------------------------------------
# Batch 138a, M11.W10a — surface completion.
#
# Eleven tools registered for cores that did not exist when Batch 131 ran the
# module's single surface wave. These tests are DISCRIMINATING on purpose:
# "the tool is in the registry" is not the load-bearing claim. Each flag is
# asserted individually (so flipping any one goes RED), and each handler is
# proven to reach the RIGHT core (so a handler wired to the wrong do_* fails
# even though every registration assertion still passes).
#
# NOT asserted here, deliberately: an Inventory-scoped exact tool count
# (``len([t for t in TOOL_REGISTRY if t.startswith("inventory_")]) == N``).
# That certification is Batch 140's and must be written in a later commit —
# a count written in the same commit as the registration it counts ratifies
# itself.
# ---------------------------------------------------------------------------

# (tool name, handler name, core symbol as imported by the handler module,
#  cacheable, admin_only, mutation, REST path, method, REST endpoint fn)
_B138A_SURFACE: list[tuple[str, str, str, bool, bool, bool, str, str, str]] = [
    (
        "inventory_record_goods_receipt",
        "handle_inventory_record_goods_receipt",
        "do_record_goods_receipt",
        False,
        True,
        True,
        "/api/inventory/record-goods-receipt",
        "POST",
        "api_inventory_record_goods_receipt",
    ),
    (
        "inventory_recommend_restock",
        "handle_inventory_recommend_restock",
        "do_recommend_restock",
        True,
        False,
        False,
        "/api/inventory/recommend-restock",
        "POST",
        "api_inventory_recommend_restock",
    ),
    (
        "inventory_forecast_demand",
        "handle_inventory_forecast_demand",
        "do_forecast_demand",
        True,
        False,
        False,
        "/api/inventory/forecast-demand",
        "POST",
        "api_inventory_forecast_demand",
    ),
    (
        "inventory_reserve_stock",
        "handle_inventory_reserve_stock",
        "do_reserve_stock",
        False,
        True,
        True,
        "/api/inventory/reserve-stock",
        "POST",
        "api_inventory_reserve_stock",
    ),
    (
        "inventory_release_stock",
        "handle_inventory_release_stock",
        "do_release_stock",
        False,
        True,
        True,
        "/api/inventory/release-stock",
        "POST",
        "api_inventory_release_stock",
    ),
    (
        "inventory_record_rma",
        "handle_inventory_record_rma",
        "do_record_rma",
        False,
        True,
        True,
        "/api/inventory/record-rma",
        "POST",
        "api_inventory_record_rma",
    ),
    (
        "inventory_valuation",
        "handle_inventory_valuation",
        "do_valuation",
        False,
        True,
        False,
        "/api/inventory/valuation",
        "GET",
        "api_inventory_valuation",
    ),
    (
        "inventory_record_goods_receipt_and_match",
        "handle_inventory_record_goods_receipt_and_match",
        "do_record_goods_receipt_and_evaluate_match",
        False,
        True,
        True,
        "/api/inventory/record-goods-receipt-and-match",
        "POST",
        "api_inventory_record_goods_receipt_and_match",
    ),
    (
        "inventory_reconcile_dead_stock",
        "handle_inventory_reconcile_dead_stock",
        "do_reconcile_dead_stock",
        False,
        True,
        False,
        "/api/inventory/reconcile-dead-stock",
        "POST",
        "api_inventory_reconcile_dead_stock",
    ),
    (
        "inventory_restock_from_rma",
        "handle_inventory_restock_from_rma",
        "do_restock_from_rma",
        False,
        True,
        True,
        "/api/inventory/restock-from-rma",
        "POST",
        "api_inventory_restock_from_rma",
    ),
    (
        "inventory_dispose_rma_weee",
        "handle_inventory_dispose_rma_weee",
        "do_dispose_rma_weee",
        False,
        True,
        True,
        "/api/inventory/dispose-rma-weee",
        "POST",
        "api_inventory_dispose_rma_weee",
    ),
]

_B138A_IDS = [row[0] for row in _B138A_SURFACE]


@pytest.mark.parametrize("row", _B138A_SURFACE, ids=_B138A_IDS)
def test_b138a_tool_registered_with_exact_flags(row: tuple) -> None:
    """Each flag asserted INDIVIDUALLY, so flipping any one of the three
    (cacheable / admin_only / mutation) on any one tool goes RED on its own.
    """
    from nce.tool_registry import TOOL_REGISTRY

    tool, _handler, _core, cacheable, admin_only, mutation, _path, _m, _fn = row
    assert tool in TOOL_REGISTRY, f"{tool} is not registered"
    spec = TOOL_REGISTRY[tool]
    assert spec.cacheable is cacheable, f"{tool}.cacheable"
    assert spec.admin_only is admin_only, f"{tool}.admin_only"
    assert spec.mutation is mutation, f"{tool}.mutation"


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _B138A_SURFACE, ids=_B138A_IDS)
async def test_b138a_registry_entry_dispatches_to_its_own_handler(row: tuple) -> None:
    """The ToolSpec for this tool must reach THIS handler.

    ``tool_registry._h`` stores a late-binding wrapper, not the function
    object, so identity cannot be compared directly. Late binding is exactly
    what makes this assertion possible: patch the module attribute and the
    registry entry must call the patch. A ToolSpec pointing at a different
    handler name leaves the probe un-awaited and goes RED.
    """
    from nce.tool_registry import TOOL_REGISTRY
    from nce.vertical_modules.inventory import mcp_handlers as inv_mcp

    tool, handler, *_rest = row
    engine = _make_engine()
    arguments = {"namespace_id": _NAMESPACE_ID}
    probe = AsyncMock(return_value="b138a-registry-probe")

    with patch.object(inv_mcp, handler, probe):
        assert await TOOL_REGISTRY[tool].handler(engine, arguments) == "b138a-registry-probe"

    probe.assert_awaited_once_with(engine, arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _B138A_SURFACE, ids=_B138A_IDS)
async def test_b138a_handler_is_a_thin_adapter_over_the_right_core(row: tuple) -> None:
    """The load-bearing claim: this handler reaches THAT core, exactly once,
    with the caller's arguments, and serialises what it returned.

    Patching the ``do_*`` symbol AS IMPORTED BY THE HANDLER MODULE is what
    makes this discriminate: a handler wired to a different core passes every
    registration assertion above and fails only here.
    """
    from nce.vertical_modules.inventory import mcp_handlers as inv_mcp

    _tool, handler_name, core_name, *_rest = row
    engine = _make_engine()
    arguments = {"namespace_id": _NAMESPACE_ID, "probe": "b138a"}
    sentinel = {"ok": True, "probe": "b138a", "qty": Decimal("1.500")}

    core = AsyncMock(return_value=sentinel)
    with patch.object(inv_mcp, core_name, core):
        raw = await getattr(inv_mcp, handler_name)(engine, arguments)

    core.assert_awaited_once_with(engine, {"namespace_id": _NAMESPACE_ID, "probe": "b138a"})
    assert json.loads(raw) == json.loads(json.dumps(sentinel, default=str))


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _B138A_SURFACE, ids=_B138A_IDS)
async def test_b138a_handler_requires_namespace_id(row: tuple) -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.inventory import mcp_handlers as inv_mcp

    _tool, handler_name, core_name, *_rest = row
    core = AsyncMock(return_value={"ok": True})
    with patch.object(inv_mcp, core_name, core):
        with pytest.raises(McpError):
            await getattr(inv_mcp, handler_name)(MagicMock(), {})
    core.assert_not_awaited()


@pytest.mark.parametrize("row", _B138A_SURFACE, ids=_B138A_IDS)
def test_b138a_rest_route_mounted_with_method_and_endpoint(row: tuple) -> None:
    """Path + method + endpoint identity. Asserting the path alone would pass
    for a route mounted with the wrong verb or pointed at another handler.
    """
    from nce.admin_app import build_admin_routes
    from nce.admin_handlers import inventory as inv_rest

    _tool, _handler, _core, _c, _a, _m, path, method, fn_name = row
    matches = [r for r in build_admin_routes() if getattr(r, "path", None) == path]
    assert len(matches) == 1, f"expected exactly one route for {path}, got {len(matches)}"
    route = matches[0]
    assert method in route.methods, f"{path} is not mounted for {method}: {route.methods}"
    assert route.endpoint is getattr(inv_rest, fn_name)


def test_b138a_deliberately_unregistered_cores_stay_off_the_surface() -> None:
    """Three Inventory cores are NOT on the surface, each for a stated reason.

    ``do_advance_bom_line_to_delivered``: exposing it would let an admin caller
    mark a BOM line delivered with no goods receipt behind it.
    ``do_flag_stock_alerts``: already wired to the cron tick, which holds
    ``acquire_cron_lock``; a manual twin would sweep outside that lock.
    ``do_create_restock_po``: it does not take the ``(engine, params)`` core
    shape at all (open asyncpg connection, keyword-only ``idempotency_key``,
    a ``confirm`` flag and an optional ``redis_client`` whose absence turns its
    kill-switch from fail-closed to open), so no thin adapter can call it.

    This test is what makes those three rulings survive a later refactor.
    """
    from nce.tool_registry import TOOL_REGISTRY

    for absent in (
        "inventory_advance_bom_line_to_delivered",
        "inventory_flag_stock_alerts",
        "inventory_create_restock_po",
    ):
        assert absent not in TOOL_REGISTRY, (
            f"{absent} was registered; see this test's docstring "
            f"-- it is an authorization/shape ruling, not an oversight"
        )
