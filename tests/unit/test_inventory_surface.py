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
  4. Tool-count assertion reflects the +3 inventory tools (now 115).
  5. The three REST routes are mounted in the admin app and return the same
     shape as the cores, including the 409 mapping for
     ``InsufficientStockError`` and 503 when no engine is connected.

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
    return MagicMock()


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
    assert len(TOOL_REGISTRY) == 115, (
        f"Expected 115 tools (112 + 3 inventory from Batch 131), got "
        f"{len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
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

    with patch.object(admin_state, "engine", MagicMock()):
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

    with patch.object(admin_state, "engine", MagicMock()):
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

    with patch.object(admin_state, "engine", MagicMock()):
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

    with patch.object(admin_state, "engine", MagicMock()):
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

    with patch.object(admin_state, "engine", MagicMock()):
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

    with patch.object(admin_state, "engine", MagicMock()):
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
