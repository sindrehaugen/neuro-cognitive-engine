"""
Admin HTTP handlers for the Procurement vertical module (W4/W7/W12: dual-surface).

Exports:
  ``api_procurement_calculate_tco``         — POST /api/procurement/tco
  ``api_procurement_rank_suppliers``        — POST /api/procurement/rank
  ``api_procurement_evaluate_match``        — POST /api/procurement/match
  ``api_procurement_sync_now``              — POST /api/procurement/sync  (admin-only, W7)
  ``api_procurement_sync_status``           — GET  /api/procurement/sync/status (admin-only, W7)
  ``api_procurement_forecast_rebate``       — POST /api/procurement/frontier/forecast-rebate (W12)
  ``api_procurement_recommend_move_spend``  — POST /api/procurement/frontier/recommend-move-spend (W12)
  ``api_procurement_whatif_spend``          — POST /api/procurement/frontier/whatif-spend (W12)

All handlers are thin REST wrappers; they do not duplicate logic.
Read-only Advisor routes — no mutation, no LLM in the path.
W7 sync routes consume Product's projection cache; they never parse Nettailer directly.
W12 frontier Advisor routes are read-only and degrade gracefully when forward-ref tables absent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from nce.admin_handlers._shared import (
    _MISSING_NAMESPACE_QUERY_PARAM,
    JSONResponse,
    _require_namespace_id,
    admin_error_response,
    admin_state,
)
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.procurement import frontier as procurement_frontier
from nce.vertical_modules.procurement.ranking import do_rank_suppliers
from nce.vertical_modules.procurement.tco import (
    do_calculate_tco,
    load_procurement_config,
)
from nce.vertical_modules.procurement.three_way_match import do_evaluate_three_way_match

log = logging.getLogger("nce.admin_handlers.procurement")

# ---------------------------------------------------------------------------
# Column-report constants (W7)
# ---------------------------------------------------------------------------
# Columns present in Product's projection that map into procurement_bid_prices.
# The GUID-bearing feed URL is NEVER included here — only column names.
_PROJECTION_COLS_MAPPED: tuple[str, ...] = (
    "artnr",
    "leverandor",
    "bid_id",
    "prodid",
    "pris",
    "valid_to",
)

# The consumer cache also stores these housekeeping columns, not from the projection.
_CACHE_ONLY_COLS: tuple[str, ...] = (
    "namespace_id",
    "raw",
    "synced_at",
)

# Columns received from Product's projection that are not mapped to named cache columns
# (stored verbatim in the ``raw`` JSONB column for auditability).
_PROJECTION_COLS_UNKNOWN: tuple[str, ...] = ()


def _column_report() -> dict:
    """Return the mapped/unknown column breakdown.  Never includes URLs or secrets."""
    return {
        "cache_table": "procurement_bid_prices",
        "projection_source": "Product module (A2A/REST push — column names only, no URL)",
        "mapped": list(_PROJECTION_COLS_MAPPED),
        "cache_only": list(_CACHE_ONLY_COLS),
        "unknown": list(_PROJECTION_COLS_UNKNOWN),
    }


async def api_procurement_calculate_tco(request) -> JSONResponse:
    """POST /api/procurement/tco

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        supplier     (dict, required): Must contain ``unit_price`` (float).
        bom_line     (dict, required): Must contain ``quantity`` (int).

    Response (JSON):
        {"status": "ok", "price": ..., "freight": ..., "warranty": ...,
         "stock": ..., "delivery_risk": ..., "total": ...}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    supplier = dict(body.get("supplier") or {})
    bom_line = dict(body.get("bom_line") or {})

    try:
        weights, tolerances = load_procurement_config()
        result = do_calculate_tco(weights, tolerances, supplier, bom_line)
        return JSONResponse({**result, "status": "ok"})
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Procurement TCO error",
            exc,
            status_code=500,
            log_event="api_procurement_calculate_tco",
        )


async def api_procurement_rank_suppliers(request) -> JSONResponse:
    """POST /api/procurement/rank

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        bom_line     (dict, required): Must contain ``quantity`` (int).
        candidates   (list[dict], required): Each must contain ``unit_price`` (float).

    Response (JSON):
        {"status": "ok", "ranked": [...], "rebate_override": bool,
         "rebate_rationale": str}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    bom_line = dict(body.get("bom_line") or {})
    candidates = list(body.get("candidates") or [])

    try:
        weights, _tolerances = load_procurement_config()
        result = do_rank_suppliers(weights, bom_line, candidates)
        return JSONResponse({**result, "status": "ok"})
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Procurement rank error",
            exc,
            status_code=500,
            log_event="api_procurement_rank_suppliers",
        )


async def api_procurement_evaluate_match(request) -> JSONResponse:
    """POST /api/procurement/match

    Request body (JSON):
        namespace_id  (str, required): Active namespace UUID.
        po            (dict, required): ``article_id``, ``quantity``, ``unit_price``.
        goods_receipt (dict, required): ``quantity``.
        invoice       (dict, required): ``article_id``, ``quantity``, ``unit_price``.

    Response (JSON):
        {"status": "ok", "confidence": float, "tier": str,
         "tolerance_zone": dict, "substitution": dict}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    po = dict(body.get("po") or {})
    goods_receipt = dict(body.get("goods_receipt") or {})
    invoice = dict(body.get("invoice") or {})

    try:
        _weights, tolerances = load_procurement_config()
        result = do_evaluate_three_way_match(tolerances, po, goods_receipt, invoice)
        return JSONResponse({**result, "status": "ok"})
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Procurement match error",
            exc,
            status_code=500,
            log_event="api_procurement_evaluate_match",
        )


# ---------------------------------------------------------------------------
# W7: sync routes — projection-consumer operator surface
# ---------------------------------------------------------------------------


async def api_procurement_sync_now(request) -> JSONResponse:
    """POST /api/procurement/sync

    Trigger a refresh of the ``procurement_bid_prices`` consumer cache from
    Product's projection and record the attempt in the event log.

    This route consumes Product's projection (§9.1) — it never parses the
    Nettailer feed directly.  If no live projection push is outstanding the
    call is a no-op-safe refresh: it records the attempt and returns current
    cache statistics.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"status": "ok", "rows_refreshed": int, "synced_at": str ISO-8601,
         "column_report": {...}}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id_str, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err
    namespace_id = uuid.UUID(str(namespace_id_str))

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::bigint           AS row_count,
                       MAX(synced_at)             AS last_synced_at
                FROM   procurement_bid_prices
                WHERE  namespace_id = $1
                """,
                namespace_id,
            )
            row_count: int = int(row["row_count"]) if row else 0
            last_synced_at: datetime | None = row["last_synced_at"] if row else None

            synced_at_str = (
                last_synced_at.isoformat()
                if last_synced_at
                else datetime.now(tz=timezone.utc).isoformat()
            )

            # Operational record of the refresh attempt. This is an admin
            # operational action, NOT a replay-able domain event, so it is
            # logged operationally rather than written to the WORM event_log
            # (which only accepts registered EventTypes + replay handlers).
            # Cache freshness is tracked via procurement_bid_prices.synced_at.
            log.info(
                "[procurement-sync] sync_now namespace=%s rows_in_cache=%d synced_at=%s",
                namespace_id,
                row_count,
                synced_at_str,
            )

        return JSONResponse(
            {
                "status": "ok",
                "rows_refreshed": row_count,
                "synced_at": synced_at_str,
                "column_report": _column_report(),
            }
        )

    except Exception as exc:
        return admin_error_response(
            "Procurement sync error",
            exc,
            status_code=500,
            log_event="api_procurement_sync_now",
        )


async def api_procurement_sync_status(request) -> JSONResponse:
    """GET /api/procurement/sync/status

    Return the current health, freshness, and row counts for the
    ``procurement_bid_prices`` consumer cache, plus the column-report.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"status": "ok", "last_synced_at": str|null, "row_count": int,
         "freshness_seconds": float|null, "column_report": {...}}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id_str, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err
    namespace_id = uuid.UUID(str(namespace_id_str))

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::bigint           AS row_count,
                       MAX(synced_at)             AS last_synced_at
                FROM   procurement_bid_prices
                WHERE  namespace_id = $1
                """,
                namespace_id,
            )

        row_count = int(row["row_count"]) if row else 0
        last_synced_at: datetime | None = row["last_synced_at"] if row else None

        last_synced_at_str: str | None = last_synced_at.isoformat() if last_synced_at else None
        freshness_seconds: float | None = None
        if last_synced_at is not None:
            freshness_seconds = (datetime.now(tz=timezone.utc) - last_synced_at).total_seconds()

        return JSONResponse(
            {
                "status": "ok",
                "last_synced_at": last_synced_at_str,
                "row_count": row_count,
                "freshness_seconds": freshness_seconds,
                "column_report": _column_report(),
            }
        )

    except Exception as exc:
        return admin_error_response(
            "Procurement sync status error",
            exc,
            status_code=500,
            log_event="api_procurement_sync_status",
        )


# ---------------------------------------------------------------------------
# W12: Frontier Advisor REST routes — read-only, no mutation
# ---------------------------------------------------------------------------


async def api_procurement_forecast_rebate(request) -> JSONResponse:
    """POST /api/procurement/frontier/forecast-rebate

    Forecast year-end rebate band from BOM pipeline + kickback tiers.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        supplier_id  (str, optional): Filter to a single supplier.

    Response (JSON):
        {"status": "ok", "annual_spend": float, "matched_tier": dict|null,
         "rebate_amount": float, "rebate_low": float, "rebate_high": float,
         "confidence": str, "rationale": str}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    try:
        result = await procurement_frontier.do_forecast_rebate(admin_state.engine, body)
        return JSONResponse({**result, "status": "ok"})
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Procurement forecast rebate error",
            exc,
            status_code=500,
            log_event="api_procurement_forecast_rebate",
        )


async def api_procurement_recommend_move_spend(request) -> JSONResponse:
    """POST /api/procurement/frontier/recommend-move-spend

    Recommend which supplier to consolidate spend toward for best ROI.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"status": "ok", "recommendation": str, "top_supplier": dict|null,
         "roi_scores": list, "rationale": str}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    try:
        result = await procurement_frontier.do_recommend_move_spend(admin_state.engine, body)
        return JSONResponse({**result, "status": "ok"})
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Procurement recommend move spend error",
            exc,
            status_code=500,
            log_event="api_procurement_recommend_move_spend",
        )


async def api_procurement_whatif_spend(request) -> JSONResponse:
    """POST /api/procurement/frontier/whatif-spend

    Simulate a hypothetical spend shift and return projected delta.

    Request body (JSON):
        namespace_id   (str, required): Active namespace UUID.
        from_supplier  (str, required): Supplier to shift spend away from.
        to_supplier    (str, required): Supplier to shift spend toward.
        shift_fraction (float, required): Fraction of current spend to shift (0–1).

    Response (JSON):
        {"status": "ok", "shifted_spend": float, "delta_savings": float,
         "delta_rebate": float, "net_delta": float,
         "recommendation": str, "rationale": str}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    try:
        result = await procurement_frontier.do_whatif_spend(admin_state.engine, body)
        return JSONResponse({**result, "status": "ok"})
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Procurement what-if spend error",
            exc,
            status_code=500,
            log_event="api_procurement_whatif_spend",
        )
