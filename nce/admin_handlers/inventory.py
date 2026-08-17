"""
Admin HTTP handlers for the Inventory vertical module (Module 11, Wave 3 —
Batch 131, ``stock-surface``).

Exports:
  ``api_inventory_stock_levels``       — GET  /api/inventory/stock-levels
  ``api_inventory_transfer_stock``     — POST /api/inventory/transfer-stock
  ``api_inventory_record_consumption`` — POST /api/inventory/record-consumption

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
  anything else                      -> 500 via ``admin_error_response``
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
)
from nce.auth import validate_agent_id
from nce.vertical_modules.inventory.stock import (
    InsufficientStockError,
    do_record_consumption,
    do_stock_levels,
    do_transfer_stock,
)

log = logging.getLogger("nce.admin_handlers.inventory")


def _json_safe(value: Any) -> Any:
    """Round-trip *value* through a ``Decimal``-aware ``json.dumps`` so every
    exact-decimal quantity (``inventory_items`` is ``NUMERIC(18,3)`` — see
    ``stock.py``'s module docstring) becomes its exact string form before
    Starlette's own JSON encoder (which has no ``default=`` hook) ever sees
    it. Mirrors ``admin_handlers/economy.py``'s ``_json_safe`` — the "never
    coerce an exact quantity through float" discipline applies to stock
    quantities, not just money.
    """
    return json.loads(json.dumps(value, default=str))


def _require_namespace_id(raw: str | None) -> tuple[str | None, JSONResponse | None]:
    """Validate the ``namespace_id`` shared by all three routes.

    Returns ``(namespace_id, None)`` on success or ``(None, error_response)``
    on failure. ``validate_agent_id`` only sanitises free text and never
    raises (see ``nce/auth.py``), so the actual UUID-shape check is the
    explicit ``UUID(...)`` parse below (mirrors
    ``admin_handlers/economy.py``'s namespace validation).
    """
    namespace_id = str(raw or "").strip()
    if not namespace_id:
        return None, JSONResponse(
            {"error": "Missing required field: namespace_id"}, status_code=422
        )
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return None, JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)
    return namespace_id, None


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
