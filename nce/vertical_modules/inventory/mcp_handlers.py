"""
nce/vertical_modules/inventory/mcp_handlers.py
================================================
MCP tool handlers for the Inventory vertical module (Module 11, Wave 3 —
Batch 131, ``stock-surface``).

Public entry-points — thin adapters over the Wave 2 stock-core
(``nce/vertical_modules/inventory/stock.py``):

  ``handle_inventory_stock_levels``       — Watcher; read-only, cacheable.
  ``handle_inventory_transfer_stock``     — Actor; mutation, admin_only.
  ``handle_inventory_record_consumption`` — Actor; mutation, admin_only.

Flags mirror the "MCP tools" table in
``docs/vertical_engines/11-inventory-engine.md``:

| Tool                              | cacheable | admin_only | mutation |
|------------------------------------|-----------|------------|----------|
| inventory_stock_levels             | Y         | N          | N        |
| inventory_transfer_stock           | N         | Y          | Y        |
| inventory_record_consumption       | N         | Y          | Y        |

This wave registers ONLY the three stock-core entry points Wave 2 shipped
(``do_stock_levels`` / ``do_transfer_stock`` / ``do_record_consumption``).
The remaining Inventory tools (goods-receipt, restock, forecast, reserve,
RMA) are unbuilt cores and out of scope here — they land in later waves.

``InsufficientStockError`` (a business-rule refusal — "not enough stock",
never a malformed request) is caught explicitly in the two mutation
handlers and returned as a structured ``{"error": ..., "sku",
"location_id", "requested", "available_on_hand"}`` JSON payload, mirroring
``economy/mcp_handlers.py``'s treatment of ``UnbalancedPostingsError``. Left
uncaught it would fall through ``@mcp_handler``'s generic ``Exception``
branch and be mis-filed as ``MCP_INTERNAL_ERROR`` (-32603) — a normal
"not enough stock" outcome is not a server bug.

Registered in ``nce/tool_registry.py`` via
``_h(inventory_mcp_handlers, "handle_inventory_*")``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.inventory.stock import (
    InsufficientStockError,
    do_record_consumption,
    do_stock_levels,
    do_transfer_stock,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.mcp_handlers")


def _insufficient_stock_error(exc: InsufficientStockError) -> str:
    """Serialise an :class:`InsufficientStockError` as a structured JSON error.

    Never re-raised — a business-rule refusal (not enough stock) is a normal
    outcome for these Actor tools, not an internal-error condition.
    """
    return json.dumps(
        {
            "error": str(exc),
            "sku": exc.sku,
            "location_id": str(exc.location_id),
            "requested": exc.requested,
            "available_on_hand": exc.available_on_hand,
        },
        default=str,
    )


@mcp_handler
async def handle_inventory_stock_levels(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_stock_levels — live per-SKU-per-location stock (Watcher, read-only).

    Requires ``namespace_id``; optionally filters by ``sku`` and/or
    ``location``. Always reads the authoritative ``inventory_items`` row —
    never the graph mirror (see ``stock.py``'s module docstring).

    Returns ``{"ok": True, "items": [...]}``. Thin adapter — all logic lives
    in ``stock.do_stock_levels``.
    """
    require_namespace_id(arguments)
    result = await do_stock_levels(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_transfer_stock(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_transfer_stock — warehouse<->van / van<->van stock move (Actor).

    Requires ``namespace_id``, ``sku``, ``qty``, ``from_location``, and
    ``to_location``. Actor / admin-only (``mutation=True, admin_only=True``).

    Returns ``{"ok": True, "sku", "from_location", "to_location", "qty",
    "from_on_hand", "to_on_hand"}`` on success, or a structured
    ``{"error": ...}`` (see module docstring) when ``from_location`` does not
    hold enough stock. Thin adapter — all logic lives in
    ``stock.do_transfer_stock``.
    """
    require_namespace_id(arguments)
    try:
        result = await do_transfer_stock(engine, dict(arguments))
    except InsufficientStockError as exc:
        return _insufficient_stock_error(exc)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_record_consumption(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_record_consumption — pick/use stock for a job (Actor).

    Requires ``namespace_id``, ``sku``, ``qty``, ``location``; optionally
    accepts ``work_order`` (echoed back for traceability only). Actor /
    admin-only (``mutation=True, admin_only=True``).

    Returns ``{"ok": True, "sku", "location", "qty", "on_hand",
    "work_order"}`` on success, or a structured ``{"error": ...}`` (see
    module docstring) when *location* does not hold enough stock. Thin
    adapter — all logic lives in ``stock.do_record_consumption``.
    """
    require_namespace_id(arguments)
    try:
        result = await do_record_consumption(engine, dict(arguments))
    except InsufficientStockError as exc:
        return _insufficient_stock_error(exc)
    return json.dumps(result, default=str)
