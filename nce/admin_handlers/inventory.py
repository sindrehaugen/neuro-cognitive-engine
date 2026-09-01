"""
Admin HTTP handlers for the Inventory vertical module (Module 11, Wave 3 —
Batch 131, ``stock-surface``).

Exports:
  ``api_inventory_stock_levels``       — GET  /api/inventory/stock-levels
  ``api_inventory_transfer_stock``     — POST /api/inventory/transfer-stock
  ``api_inventory_record_consumption`` — POST /api/inventory/record-consumption

Batch 138a (M11.W10a, ``inventory-surface-completion``) adds the eleven
routes whose cores did not exist when Batch 131 ran the module's single
surface wave:
  ``api_inventory_record_goods_receipt``           — POST /api/inventory/record-goods-receipt
  ``api_inventory_recommend_restock``              — POST /api/inventory/recommend-restock
  ``api_inventory_forecast_demand``                — POST /api/inventory/forecast-demand
  ``api_inventory_reserve_stock``                  — POST /api/inventory/reserve-stock
  ``api_inventory_release_stock``                  — POST /api/inventory/release-stock
  ``api_inventory_record_rma``                     — POST /api/inventory/record-rma
  ``api_inventory_valuation``                      — GET  /api/inventory/valuation
  ``api_inventory_record_goods_receipt_and_match`` — POST /api/inventory/record-goods-receipt-and-match
  ``api_inventory_reconcile_dead_stock``           — POST /api/inventory/reconcile-dead-stock
  ``api_inventory_restock_from_rma``               — POST /api/inventory/restock-from-rma
  ``api_inventory_dispose_rma_weee``               — POST /api/inventory/dispose-rma-weee

All handlers are thin REST wrappers over the Wave 2 stock-core
(``nce/vertical_modules/inventory/stock.py``); they contain no business
logic, no LLM in the path. ``api_inventory_stock_levels`` is the "own stock
first" read Procurement's scoring calls, and the no-model path for the BFF
warehouse/van-stock screen (see
``docs/vertical_engines/11-inventory-engine.md``'s "REST routes" section).

Error mapping:
  missing/invalid ``namespace_id``   -> 422
  ``ValueError`` from the core       -> 422 (malformed params, e.g. bad qty
                                        or a self-transfer)
  ``InsufficientStockError``         -> 409 (business-rule refusal — not
                                        enough stock; distinct from a
                                        malformed request)
  any ``refusals.BUSINESS_REFUSALS`` -> 409 with a machine-readable
                                        ``reason`` (see below)
  anything else                      -> 500 via ``admin_error_response``

That gap is CLOSED (debt item **D38**). Batch 138a filed six domain refusals
this module had no precedent status code for — ``InsufficientAvailableError``
(reserve), ``OverReleaseError`` (release), ``RmaNotFoundError`` /
``RmaAlreadySettledError`` / ``RmaNotWeeeScopeError`` (RMA legs) and
``LedgerDivergenceError`` (dead-stock reconcile). All are bare ``Exception``
subclasses, so none was absorbed by the ``ValueError`` arm and each fell to
``admin_error_response`` (500) — indistinguishable, to a client, from "the
backend is down", so the rational response was to retry a call that could
only ever fail the same way.

The orchestrator owned that error-contract decision and took option (b) of
``FE_UPDATE_2026-08-31_ADDENDUM.md``: ONE shared refusal status carrying a
machine-readable ``reason``. The mapping is declared exactly once, in
``inventory/refusals.py`` (D18 precedent: three instances of one defect class
means one shared mapping, not a sixth bespoke ``except``), and every affected
route carries a single ``except BUSINESS_REFUSALS`` clause that delegates to
it. Adding a seventh refusal is a row in that table, not an edit on two
surfaces.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.admin_handlers._shared import (
    JSONResponse,
    _json_safe,
    _require_namespace_id,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
)
from nce.vertical_modules.inventory.forecast import do_forecast_demand
from nce.vertical_modules.inventory.goods_receipt import do_record_goods_receipt
from nce.vertical_modules.inventory.reconcile import do_reconcile_dead_stock
from nce.vertical_modules.inventory.refusals import (
    BUSINESS_REFUSALS,
    REST_BUSINESS_REFUSED_STATUS,
    refusal_payload,
)
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

log = logging.getLogger("nce.admin_handlers.inventory")


def _insufficient_stock_response(exc: InsufficientStockError) -> JSONResponse:
    return JSONResponse(
        _json_safe(
            {
                "error": str(exc),
                "sku": exc.sku,
                "location_id": str(exc.location_id),
                "requested": exc.requested,
                "available_on_hand": exc.available_on_hand,
            }
        ),
        status_code=409,
    )


# ---------------------------------------------------------------------------
# GET /api/inventory/stock-levels
# ---------------------------------------------------------------------------


async def api_inventory_stock_levels(request) -> JSONResponse:
    """GET /api/inventory/stock-levels

    Live stock per SKU per location — the "own stock first" read Procurement's
    scoring calls, and the BFF warehouse/van-stock screen.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        sku          (str, optional): filter to one SKU.
        location     (str, optional): filter to one stock_locations id.

    Response (JSON):
        {"ok": True, "items": [{"sku", "location_id", "on_hand", "reserved",
         "blocked", "available"}, ...]}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {"namespace_id": namespace_id}
    sku = request.query_params.get("sku")
    if sku is not None:
        params["sku"] = sku
    location = request.query_params.get("location")
    if location is not None:
        params["location"] = location

    try:
        result = await do_stock_levels(admin_state.engine, params)
        return JSONResponse(_json_safe(result))
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory stock-levels error",
            exc,
            status_code=500,
            log_event="api_inventory_stock_levels",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/transfer-stock
# ---------------------------------------------------------------------------


async def api_inventory_transfer_stock(request) -> JSONResponse:
    """POST /api/inventory/transfer-stock

    Move stock between two locations (warehouse<->van / van<->van).

    Request body (JSON):
        namespace_id  (str, required): Active namespace UUID.
        sku           (str, required)
        qty           (int | float | str, required): > 0.
        from_location (str, required): a stock_locations id.
        to_location   (str, required): a stock_locations id.

    Response (JSON) — success:
        {"ok": True, "sku", "from_location", "to_location", "qty",
         "from_on_hand", "to_on_hand"}  HTTP 200
    Response (JSON) — not enough stock at from_location:
        {"error": ..., "sku", "location_id", "requested",
         "available_on_hand"}  HTTP 409
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "sku": body.get("sku"),
        "qty": body.get("qty"),
        "from_location": body.get("from_location"),
        "to_location": body.get("to_location"),
    }

    try:
        result = await do_transfer_stock(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads
        # (inventory_stock_levels) now the transfer has committed.
        await bump_mcp_cache_generation(admin_state.engine, route="api_inventory_transfer_stock")
        return JSONResponse(_json_safe(result))
    except InsufficientStockError as exc:
        return _insufficient_stock_response(exc)
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory transfer-stock error",
            exc,
            status_code=500,
            log_event="api_inventory_transfer_stock",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/record-consumption
# ---------------------------------------------------------------------------


async def api_inventory_record_consumption(request) -> JSONResponse:
    """POST /api/inventory/record-consumption

    Decrement stock when it is picked/used at one location (e.g. a Field
    Tech work-order).

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        sku          (str, required)
        qty          (int | float | str, required): > 0.
        location     (str, required): a stock_locations id.
        work_order   (str, optional): pass-through reference.

    Response (JSON) — success:
        {"ok": True, "sku", "location", "qty", "on_hand", "work_order"}  HTTP 200
    Response (JSON) — not enough stock at location:
        {"error": ..., "sku", "location_id", "requested",
         "available_on_hand"}  HTTP 409
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "sku": body.get("sku"),
        "qty": body.get("qty"),
        "location": body.get("location"),
        "work_order": body.get("work_order"),
    }

    try:
        result = await do_record_consumption(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads
        # (inventory_stock_levels) now the consumption has committed.
        await bump_mcp_cache_generation(
            admin_state.engine, route="api_inventory_record_consumption"
        )
        return JSONResponse(_json_safe(result))
    except InsufficientStockError as exc:
        return _insufficient_stock_response(exc)
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory record-consumption error",
            exc,
            status_code=500,
            log_event="api_inventory_record_consumption",
        )


# ---------------------------------------------------------------------------
# Batch 138a, M11.W10a — surface completion. Eleven thin REST adapters over
# cores that did not exist when Batch 131 ran the module's single surface wave.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /api/inventory/record-goods-receipt
# ---------------------------------------------------------------------------


async def api_inventory_record_goods_receipt(request) -> JSONResponse:
    """POST /api/inventory/record-goods-receipt

    Record one inbound delivery: idempotent capture, authoritative stock
    increment and costed ledger append, all in one transaction.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``po_ref``, ``location_id``, ``lines``, ``delivery_note_ref``, ``scans``.

    Thin adapter over ``do_record_goods_receipt`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "po_ref": body.get("po_ref"),
        "location_id": body.get("location_id"),
        "lines": body.get("lines"),
        "delivery_note_ref": body.get("delivery_note_ref"),
        "scans": body.get("scans"),
    }

    try:
        result = await do_record_goods_receipt(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads now the
        # mutation has committed.
        await bump_mcp_cache_generation(
            admin_state.engine, route="api_inventory_record_goods_receipt"
        )
        return JSONResponse(_json_safe(result))
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory record-goods-receipt error",
            exc,
            status_code=500,
            log_event="api_inventory_record_goods_receipt",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/recommend-restock
# ---------------------------------------------------------------------------


async def api_inventory_recommend_restock(request) -> JSONResponse:
    """POST /api/inventory/recommend-restock

    Per-SKU restock recommendations from ledger-derived stock position and
    consumption velocity. Writes nothing.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``location``, ``sku``.

    Thin adapter over ``do_recommend_restock`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "location": body.get("location"),
        "sku": body.get("sku"),
    }

    try:
        result = await do_recommend_restock(admin_state.engine, params)
        return JSONResponse(_json_safe(result))
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory recommend-restock error",
            exc,
            status_code=500,
            log_event="api_inventory_recommend_restock",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/forecast-demand
# ---------------------------------------------------------------------------


async def api_inventory_forecast_demand(request) -> JSONResponse:
    """POST /api/inventory/forecast-demand

    Pipeline-weighted demand forecast per SKU. ``horizon_days`` both echoes
    back and filters which pipeline lines count. Writes nothing.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``horizon_days``, ``sku``.

    Thin adapter over ``do_forecast_demand`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "horizon_days": body.get("horizon_days"),
        "sku": body.get("sku"),
    }

    try:
        result = await do_forecast_demand(admin_state.engine, params)
        return JSONResponse(_json_safe(result))
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory forecast-demand error",
            exc,
            status_code=500,
            log_event="api_inventory_forecast_demand",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/reserve-stock
# ---------------------------------------------------------------------------


async def api_inventory_reserve_stock(request) -> JSONResponse:
    """POST /api/inventory/reserve-stock

    Reserve available stock against a project. Increments ``qty_reserved``
    only; no physical stock moves.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``sku``, ``qty``, ``location``, ``project_id``.

    Thin adapter over ``do_reserve_stock`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "sku": body.get("sku"),
        "qty": body.get("qty"),
        "location": body.get("location"),
        "project_id": body.get("project_id"),
    }

    try:
        result = await do_reserve_stock(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads now the
        # mutation has committed.
        await bump_mcp_cache_generation(admin_state.engine, route="api_inventory_reserve_stock")
        return JSONResponse(_json_safe(result))
    except BUSINESS_REFUSALS as exc:
        return JSONResponse(
            _json_safe(refusal_payload(exc)),
            status_code=REST_BUSINESS_REFUSED_STATUS,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory reserve-stock error",
            exc,
            status_code=500,
            log_event="api_inventory_reserve_stock",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/release-stock
# ---------------------------------------------------------------------------


async def api_inventory_release_stock(request) -> JSONResponse:
    """POST /api/inventory/release-stock

    Release previously-reserved stock. Decrements ``qty_reserved`` only.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``sku``, ``qty``, ``location``, ``project_id``.

    Thin adapter over ``do_release_stock`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "sku": body.get("sku"),
        "qty": body.get("qty"),
        "location": body.get("location"),
        "project_id": body.get("project_id"),
    }

    try:
        result = await do_release_stock(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads now the
        # mutation has committed.
        await bump_mcp_cache_generation(admin_state.engine, route="api_inventory_release_stock")
        return JSONResponse(_json_safe(result))
    except BUSINESS_REFUSALS as exc:
        return JSONResponse(
            _json_safe(refusal_payload(exc)),
            status_code=REST_BUSINESS_REFUSED_STATUS,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory release-stock error",
            exc,
            status_code=500,
            log_event="api_inventory_release_stock",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/record-rma
# ---------------------------------------------------------------------------


async def api_inventory_record_rma(request) -> JSONResponse:
    """POST /api/inventory/record-rma

    Record a return with its WEEE disposal compliance state. Moves no stock;
    ``stock_movement_state`` is written ``'pending'``.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``rma_ref``, ``sku``, ``location``, ``qty``, ``reason``, ``serial``, ``weee_state``, ``disposal_ref``.

    Thin adapter over ``do_record_rma`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "rma_ref": body.get("rma_ref"),
        "sku": body.get("sku"),
        "location": body.get("location"),
        "qty": body.get("qty"),
        "reason": body.get("reason"),
        "serial": body.get("serial"),
        "weee_state": body.get("weee_state"),
        "disposal_ref": body.get("disposal_ref"),
    }

    try:
        result = await do_record_rma(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads now the
        # mutation has committed.
        await bump_mcp_cache_generation(admin_state.engine, route="api_inventory_record_rma")
        return JSONResponse(_json_safe(result))
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory record-rma error",
            exc,
            status_code=500,
            log_event="api_inventory_record_rma",
        )


# ---------------------------------------------------------------------------
# GET /api/inventory/valuation
# ---------------------------------------------------------------------------


async def api_inventory_valuation(request) -> JSONResponse:
    """GET /api/inventory/valuation

    FIFO/average-cost money value of one (sku, location)'s stock, computed
    from the append-only ``inventory_transactions`` ledger. Reads only.

    Query parameters: ``namespace_id`` (required, namespace UUID), plus
    ``sku``, ``location``.

    Thin adapter over ``do_valuation`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {"namespace_id": namespace_id}
    params["sku"] = request.query_params.get("sku")
    params["location"] = request.query_params.get("location")

    try:
        result = await do_valuation(admin_state.engine, params)
        return JSONResponse(_json_safe(result))
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory valuation error",
            exc,
            status_code=500,
            log_event="api_inventory_valuation",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/record-goods-receipt-and-match
# ---------------------------------------------------------------------------


async def api_inventory_record_goods_receipt_and_match(request) -> JSONResponse:
    """POST /api/inventory/record-goods-receipt-and-match

    Record one inbound delivery AND fire the three-way match on it, once.
    A caller with no invoice yet must use /api/inventory/record-goods-receipt
    instead: a receipt recorded that way can never afterwards be matched
    through this route once the invoice arrives, silently.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``po_ref``, ``location_id``, ``lines``, ``delivery_note_ref``, ``scans``, ``po``, ``invoice``.

    Thin adapter over ``do_record_goods_receipt_and_evaluate_match`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "po_ref": body.get("po_ref"),
        "location_id": body.get("location_id"),
        "lines": body.get("lines"),
        "delivery_note_ref": body.get("delivery_note_ref"),
        "scans": body.get("scans"),
        "po": body.get("po"),
        "invoice": body.get("invoice"),
    }

    try:
        result = await do_record_goods_receipt_and_evaluate_match(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads now the
        # mutation has committed.
        await bump_mcp_cache_generation(
            admin_state.engine, route="api_inventory_record_goods_receipt_and_match"
        )
        return JSONResponse(_json_safe(result))
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory record-goods-receipt-and-match error",
            exc,
            status_code=500,
            log_event="api_inventory_record_goods_receipt_and_match",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/reconcile-dead-stock
# ---------------------------------------------------------------------------


async def api_inventory_reconcile_dead_stock(request) -> JSONResponse:
    """POST /api/inventory/reconcile-dead-stock

    Reconcile every dead ``(sku, location)`` pair against the ledger. Writes
    nothing at all, on the clean path and the raising path alike.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``dead_stock_days``.

    Thin adapter over ``do_reconcile_dead_stock`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "dead_stock_days": body.get("dead_stock_days"),
    }

    try:
        result = await do_reconcile_dead_stock(admin_state.engine, params)
        return JSONResponse(_json_safe(result))
    except BUSINESS_REFUSALS as exc:
        return JSONResponse(
            _json_safe(refusal_payload(exc)),
            status_code=REST_BUSINESS_REFUSED_STATUS,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory reconcile-dead-stock error",
            exc,
            status_code=500,
            log_event="api_inventory_reconcile_dead_stock",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/restock-from-rma
# ---------------------------------------------------------------------------


async def api_inventory_restock_from_rma(request) -> JSONResponse:
    """POST /api/inventory/restock-from-rma

    Return a repairable unit to stock at the RMA's own location. ``sku``,
    ``location_id`` and ``qty`` come from the claimed row, not the caller.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``rma_ref``.

    Thin adapter over ``do_restock_from_rma`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "rma_ref": body.get("rma_ref"),
    }

    try:
        result = await do_restock_from_rma(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads now the
        # mutation has committed.
        await bump_mcp_cache_generation(admin_state.engine, route="api_inventory_restock_from_rma")
        return JSONResponse(_json_safe(result))
    except BUSINESS_REFUSALS as exc:
        return JSONResponse(
            _json_safe(refusal_payload(exc)),
            status_code=REST_BUSINESS_REFUSED_STATUS,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory restock-from-rma error",
            exc,
            status_code=500,
            log_event="api_inventory_restock_from_rma",
        )


# ---------------------------------------------------------------------------
# POST /api/inventory/dispose-rma-weee
# ---------------------------------------------------------------------------


async def api_inventory_dispose_rma_weee(request) -> JSONResponse:
    """POST /api/inventory/dispose-rma-weee

    Permanently remove a WEEE-scope return from stock under the approved
    take-back scheme. Stock leaves and the ledger row proves it happened.

    Request body (JSON): ``namespace_id`` (required, namespace UUID), plus
    ``rma_ref``, ``disposal_ref``.

    Thin adapter over ``do_dispose_rma_weee`` — no business logic here.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "rma_ref": body.get("rma_ref"),
        "disposal_ref": body.get("disposal_ref"),
    }

    try:
        result = await do_dispose_rma_weee(admin_state.engine, params)
        # Mirror the MCP dispatch loop: invalidate cached reads now the
        # mutation has committed.
        await bump_mcp_cache_generation(admin_state.engine, route="api_inventory_dispose_rma_weee")
        return JSONResponse(_json_safe(result))
    except InsufficientStockError as exc:
        return _insufficient_stock_response(exc)
    except BUSINESS_REFUSALS as exc:
        return JSONResponse(
            _json_safe(refusal_payload(exc)),
            status_code=REST_BUSINESS_REFUSED_STATUS,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Inventory dispose-rma-weee error",
            exc,
            status_code=500,
            log_event="api_inventory_dispose_rma_weee",
        )
