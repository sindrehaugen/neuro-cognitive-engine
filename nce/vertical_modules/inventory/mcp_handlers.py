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
| inventory_record_goods_receipt     | N         | Y          | Y        |
| inventory_recommend_restock        | Y         | N          | N        |
| inventory_forecast_demand          | Y         | N          | N        |
| inventory_reserve_stock            | N         | Y          | Y        |
| inventory_release_stock            | N         | Y          | Y        |
| inventory_record_rma               | N         | Y          | Y        |
| inventory_valuation                | N         | Y          | N        |
| inventory_record_goods_receipt_and_match | N   | Y          | Y        |
| inventory_reconcile_dead_stock     | N         | Y          | N        |
| inventory_restock_from_rma         | N         | Y          | Y        |
| inventory_dispose_rma_weee         | N         | Y          | Y        |

Batch 131 (Wave 3) registered ONLY the three stock-core entry points Wave 2
had shipped; the plan gives each module a single surface wave, placed before
most of its cores exist. Batch 138a (M11.W10a, ``inventory-surface-completion``)
registers the eleven later cores — goods receipt, replenishment, forecast,
reservation, RMA, valuation, dead-stock reconcile, and the goods-receipt +
three-way-match composition. It is registration only: no core changed.

Two cores are DELIBERATELY not registered, by orchestrator ruling:
``do_advance_bom_line_to_delivered`` (registering it would let an admin
caller mark a BOM line delivered with no goods receipt behind it) and
``do_flag_stock_alerts`` (already wired to the cron tick, which holds
``acquire_cron_lock``; a manually invocable twin would sweep outside it).

Both goods-receipt entry points ARE registered, on purpose.
``do_record_goods_receipt`` remains THE way to record a delivery when no
invoice is in hand; ``do_record_goods_receipt_and_evaluate_match`` is the
composition for callers holding all three legs at once. Two contracts, not
one with a wrapper.

``do_create_restock_po`` is NOT registered by this wave: it does not take the
``(engine, params)`` core shape. It takes an open ``asyncpg`` connection, a
keyword-only ``idempotency_key``, a ``confirm`` flag and an optional
``redis_client`` whose absence turns its kill-switch from fail-closed to
open. No thin adapter can supply those without holding a transaction and
deriving policy state, which this layer must not do.

``InsufficientStockError`` (a business-rule refusal — "not enough stock",
never a malformed request) is caught explicitly in the two mutation
handlers and returned as a structured ``{"error": ..., "sku",
"location_id", "requested", "available_on_hand"}`` JSON payload, mirroring
``economy/mcp_handlers.py``'s treatment of ``UnbalancedPostingsError``. Left
uncaught it would fall through ``@mcp_handler``'s generic ``Exception``
branch and be mis-filed as ``MCP_INTERNAL_ERROR`` (-32603) — a normal
"not enough stock" outcome is not a server bug.

The six OTHER business refusals the Batch 138a cores raise —
``InsufficientAvailableError`` (reserve), ``OverReleaseError`` (release),
``RmaNotFoundError`` / ``RmaAlreadySettledError`` / ``RmaNotWeeeScopeError``
(the RMA claim legs) and ``LedgerDivergenceError`` (dead-stock reconcile) —
were ALSO bare ``Exception`` subclasses falling through to
``MCP_INTERNAL_ERROR``. That is debt item **D38**, closed by mapping all six
through ONE shared table, ``inventory/refusals.py``, to
``McpError(-32005)`` with a machine-readable ``data.reason`` — the same shape
B140a's opt-in gate already emits. Each affected handler carries a single
``except BUSINESS_REFUSALS`` clause that delegates; none names an individual
refusal class (D18 precedent).

Note the deliberate asymmetry that remains: ``InsufficientStockError`` is
still returned as a 200-shaped payload, while these six are raised as typed
errors. Unifying them is an FE-visible breaking change to a shipped surface,
so it needs the FE's sign-off and follows this wave rather than joining it.

Registered in ``nce/tool_registry.py`` via
``_h(inventory_mcp_handlers, "handle_inventory_*")``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import McpError, mcp_handler
from nce.vertical_modules.inventory._guard import (
    InventoryDisabledError,
    require_inventory_enabled,
)
from nce.vertical_modules.inventory.forecast import do_forecast_demand
from nce.vertical_modules.inventory.goods_receipt import do_record_goods_receipt
from nce.vertical_modules.inventory.reconcile import do_reconcile_dead_stock
from nce.vertical_modules.inventory.refusals import BUSINESS_REFUSALS, mcp_refusal
from nce.vertical_modules.inventory.replenishment import do_recommend_restock
from nce.vertical_modules.inventory.reservation import do_release_stock, do_reserve_stock
from nce.vertical_modules.inventory.rma import (
    do_dispose_rma_weee,
    do_record_rma,
    do_restock_from_rma,
)
from nce.vertical_modules.inventory.stock import (
    InsufficientStockError,
    do_record_consumption,
    do_stock_levels,
    do_transfer_stock,
)
from nce.vertical_modules.inventory.transactions import do_valuation
from nce.vertical_modules.inventory.triggers import (
    do_record_goods_receipt_and_evaluate_match,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.mcp_handlers")


# ---------------------------------------------------------------------------
# Shared opt-in guard -- applied at handler boundary (not inside do_* cores)
# ---------------------------------------------------------------------------

_MCP_INVENTORY_DISABLED_CODE: int = -32005  # MCP_SCOPE_FORBIDDEN


async def _check_inventory_enabled(engine: NCEEngine, arguments: dict[str, Any]) -> None:
    """Check namespace opt-in; raise McpError(-32005) if not enabled.

    Also raises the pre-existing ``ValueError("namespace_id is required")``
    (via ``require_namespace_id``) when ``namespace_id`` is absent -- this
    function does not swallow it, so every handler keeps the exact
    missing-namespace_id behaviour Batch 131/138a shipped.

    Every ``handle_inventory_*`` handler calls this in place of its bare
    ``require_namespace_id(arguments)``, OUTSIDE the narrow ``try:`` that
    exists only to catch ``InsufficientStockError`` -- so the ``McpError``
    propagates to ``@mcp_handler`` structured, never flattened into a
    returned ``{"error": ...}`` JSON string.
    """
    namespace_id = require_namespace_id(arguments)
    try:
        await require_inventory_enabled(engine.pg_pool, namespace_id)
    except InventoryDisabledError as exc:
        raise McpError(
            _MCP_INVENTORY_DISABLED_CODE,
            "Inventory vertical is not enabled for this namespace",
            data={"reason": "inventory_disabled", "detail": str(exc)},
        ) from exc


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
    await _check_inventory_enabled(engine, arguments)
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
    await _check_inventory_enabled(engine, arguments)
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
    await _check_inventory_enabled(engine, arguments)
    try:
        result = await do_record_consumption(engine, dict(arguments))
    except InsufficientStockError as exc:
        return _insufficient_stock_error(exc)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Batch 138a, M11.W10a — surface completion. Eleven thin adapters over cores
# that did not exist when Batch 131 ran the module's single surface wave.
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_inventory_record_goods_receipt(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: inventory_record_goods_receipt — record one inbound delivery (Actor).

    Requires ``namespace_id``, ``po_ref``, ``location_id`` and ``lines``;
    optionally accepts ``delivery_note_ref`` and ``scans``. Actor / admin-only
    (``mutation=True, admin_only=True``).

    Returns the core's payload, including its ``{"ok": True, "duplicate":
    True, ...}`` replay shape. Thin adapter — all logic lives in
    ``goods_receipt.do_record_goods_receipt``.
    """
    await _check_inventory_enabled(engine, arguments)
    result = await do_record_goods_receipt(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_recommend_restock(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_recommend_restock — per-SKU restock recommendations (Watcher).

    Requires ``namespace_id``; optionally filters by ``location`` and/or
    ``sku``. Read-only and cacheable (``mutation=False, admin_only=False``);
    the core writes nothing.

    Returns ``{"ok": True, "recommendations": [...]}``. Thin adapter — all
    logic lives in ``replenishment.do_recommend_restock``.
    """
    await _check_inventory_enabled(engine, arguments)
    result = await do_recommend_restock(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_forecast_demand(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_forecast_demand — pipeline-weighted demand forecast (Watcher).

    Requires ``namespace_id``; optionally accepts ``horizon_days`` (which
    genuinely filters which pipeline lines count, since Batch 135b) and
    ``sku``. Read-only and cacheable; the core writes nothing.

    Returns ``{"ok": True, "forecasts": [...]}``. Thin adapter — all logic
    lives in ``forecast.do_forecast_demand``.
    """
    await _check_inventory_enabled(engine, arguments)
    result = await do_forecast_demand(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_reserve_stock(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_reserve_stock — reserve available stock for a project (Actor).

    Requires ``namespace_id``, ``sku``, ``qty``, ``location`` and
    ``project_id``. Actor / admin-only (``mutation=True, admin_only=True``).
    Increments ``qty_reserved`` only — no physical stock moves.

    Thin adapter — all logic lives in ``reservation.do_reserve_stock``.
    """
    await _check_inventory_enabled(engine, arguments)
    try:
        result = await do_reserve_stock(engine, dict(arguments))
    except BUSINESS_REFUSALS as exc:
        raise mcp_refusal(exc) from exc
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_release_stock(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_release_stock — release previously-reserved stock (Actor).

    Requires ``namespace_id``, ``sku``, ``qty``, ``location`` and
    ``project_id``. Actor / admin-only (``mutation=True, admin_only=True``).
    Decrements ``qty_reserved`` only.

    Thin adapter — all logic lives in ``reservation.do_release_stock``.
    """
    await _check_inventory_enabled(engine, arguments)
    try:
        result = await do_release_stock(engine, dict(arguments))
    except BUSINESS_REFUSALS as exc:
        raise mcp_refusal(exc) from exc
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_record_rma(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_record_rma — record a return with its WEEE state (Actor).

    Requires ``namespace_id``, ``rma_ref``, ``sku``, ``location``, ``qty``
    and ``reason``; optionally ``serial``, ``weee_state`` and
    ``disposal_ref``. Actor / admin-only (``mutation=True, admin_only=True``).
    Moves NO stock — ``stock_movement_state`` is written ``'pending'``.

    Thin adapter — all logic lives in ``rma.do_record_rma``.
    """
    await _check_inventory_enabled(engine, arguments)
    result = await do_record_rma(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_valuation(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_valuation — FIFO/average-cost money value of stock (Watcher).

    Requires ``namespace_id``, ``sku`` and ``location``. Read-only
    (``mutation=False``) but ``admin_only=True``: it returns cost/money data.
    NOT cacheable — it is derived from the append-only
    ``inventory_transactions`` ledger and changes on every movement, and a
    stale money figure is a wrong number in someone's accounts.

    Thin adapter — all logic lives in ``transactions.do_valuation``.
    """
    await _check_inventory_enabled(engine, arguments)
    result = await do_valuation(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_record_goods_receipt_and_match(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: inventory_record_goods_receipt_and_match — receipt + three-way match.

    Everything ``inventory_record_goods_receipt`` accepts, PLUS the ``po`` and
    ``invoice`` legs Inventory does not own. Actor / admin-only
    (``mutation=True, admin_only=True``).

    Distinct from ``inventory_record_goods_receipt``, not a wrapper: a caller
    with no invoice yet must use the plain tool. Known limitation carried from
    the core — a receipt recorded through the plain path can never afterwards
    be matched through this composition once the invoice arrives, silently.

    Thin adapter — all logic lives in
    ``triggers.do_record_goods_receipt_and_evaluate_match``.
    """
    await _check_inventory_enabled(engine, arguments)
    result = await do_record_goods_receipt_and_evaluate_match(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_reconcile_dead_stock(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: inventory_reconcile_dead_stock — reconcile dead pairs against the ledger.

    Requires ``namespace_id``; optionally ``dead_stock_days``.
    ``admin_only=True`` and ``mutation=False`` — the core writes nothing at
    all, on the clean path and the raising path alike.

    Thin adapter — all logic lives in ``reconcile.do_reconcile_dead_stock``.
    """
    await _check_inventory_enabled(engine, arguments)
    try:
        result = await do_reconcile_dead_stock(engine, dict(arguments))
    except BUSINESS_REFUSALS as exc:
        raise mcp_refusal(exc) from exc
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_restock_from_rma(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_restock_from_rma — return a repairable unit to stock (Actor).

    Requires ``namespace_id`` and ``rma_ref``; ``sku``, ``location_id`` and
    ``qty`` are read from the claimed ``inventory_rma`` row, never from the
    caller. Actor / admin-only (``mutation=True, admin_only=True``).

    Thin adapter — all logic lives in ``rma.do_restock_from_rma``.
    """
    await _check_inventory_enabled(engine, arguments)
    try:
        result = await do_restock_from_rma(engine, dict(arguments))
    except BUSINESS_REFUSALS as exc:
        raise mcp_refusal(exc) from exc
    return json.dumps(result, default=str)


@mcp_handler
async def handle_inventory_dispose_rma_weee(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: inventory_dispose_rma_weee — WEEE take-back disposal leg (Actor).

    Requires ``namespace_id``, ``rma_ref`` and ``disposal_ref``. Actor /
    admin-only (``mutation=True, admin_only=True``). Stock leaves and the
    ledger row is what proves it happened.

    Returns a structured ``{"error": ...}`` payload (see module docstring)
    when the claimed location does not hold enough stock — the same
    ``InsufficientStockError`` treatment ``handle_inventory_transfer_stock``
    already uses. Thin adapter — all logic lives in ``rma.do_dispose_rma_weee``.
    """
    await _check_inventory_enabled(engine, arguments)
    try:
        result = await do_dispose_rma_weee(engine, dict(arguments))
    except InsufficientStockError as exc:
        return _insufficient_stock_error(exc)
    except BUSINESS_REFUSALS as exc:
        raise mcp_refusal(exc) from exc
    return json.dumps(result, default=str)
