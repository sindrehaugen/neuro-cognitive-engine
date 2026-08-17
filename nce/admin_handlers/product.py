"""
Admin HTTP handlers for the Product vertical module (W3: search-get-graph;
W8: enrichment review queue).

Exports:
  ``api_product_search``             — GET  /api/product/search
  ``api_product_get``                — GET  /api/product/{id}
  ``api_product_enrichment_review``  — GET  /api/product/enrichment/review

All handlers are thin REST wrappers; they do not duplicate logic.
None return cost, margin, or BID data (ADR-0017).
"""

from __future__ import annotations

from uuid import UUID

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
    serialize_pg_row,
)
from nce.auth import validate_agent_id
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.product._guard import ProductDisabledError, require_product_enabled
from nce.vertical_modules.product.mcp_handlers import do_get_product, do_search_products

# ---------------------------------------------------------------------------
# Shared opt-in guard — applied at REST route boundary (not inside do_* cores)
# ---------------------------------------------------------------------------


async def _check_product_enabled_rest(namespace_id: str) -> JSONResponse | None:
    """Return a 409 JSONResponse when product vertical is not enabled; else None."""
    try:
        await require_product_enabled(admin_state.engine.pg_pool, namespace_id)
        return None
    except ProductDisabledError as exc:
        return JSONResponse(
            {"error": "Product vertical is not enabled for this namespace", "detail": str(exc)},
            status_code=409,
        )


async def api_product_search(request) -> JSONResponse:
    """GET /api/product/search

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        query        (str, required): Search term.
        limit        (int, optional): Maximum rows to return (default 20, max 50).

    Response (JSON):
        {"status": "ok", "results": [...], "total": <int>}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = request.query_params.get("namespace_id", "").strip()
    query = request.query_params.get("query", "").strip()
    raw_limit = request.query_params.get("limit", "20")

    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )
    if not query:
        return JSONResponse({"error": "Missing required query param: query"}, status_code=422)

    # Validate the UUID shape at the REST boundary, BEFORE the opt-in gate:
    # `validate_agent_id` only sanitizes free text and never raises (see
    # nce/auth.py), so it cannot catch a malformed namespace_id. Without this
    # explicit check, `_check_product_enabled_rest` -> `require_product_enabled`
    # would hand the raw string to asyncpg's `::uuid` cast, which raises
    # asyncpg.exceptions.DataError (not ValueError) and escapes uncaught.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    disabled = await _check_product_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 20

    try:
        result = await do_search_products(
            admin_state.engine,
            {"namespace_id": namespace_id, "query": query, "limit": limit},
        )
        # Envelope key last: if `result` ever carried a caller-influenced
        # "status" key, splatting it first would let that key win over the
        # literal below. Reversed order guarantees "status": "ok" always wins.
        return JSONResponse({**result, "status": "ok"})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Product search error",
            exc,
            status_code=500,
            log_event="api_product_search",
        )


async def api_product_get(request) -> JSONResponse:
    """GET /api/product/{id}

    Path parameter:
        id (str): mfr_part_no of the product.

    Query parameters:
        namespace_id  (str, required): Active namespace UUID.
        manufacturer  (str, optional): Disambiguate when multiple manufacturers
                       share the same part number.

    Response (JSON):
        {"status": "ok", "product": {...}|null, "prices": [...], "edges": [...]}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    mfr_part_no = request.path_params.get("id", "").strip()
    namespace_id = request.query_params.get("namespace_id", "").strip()
    manufacturer = request.query_params.get("manufacturer", "").strip() or None

    if not mfr_part_no:
        return JSONResponse({"error": "Path param 'id' (mfr_part_no) is required"}, status_code=422)
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    # See api_product_search: explicit UUID check must precede the opt-in
    # gate — validate_agent_id() never raises, so it cannot do this job.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    disabled = await _check_product_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    params: dict[str, object] = {
        "namespace_id": namespace_id,
        "mfr_part_no": mfr_part_no,
    }
    if manufacturer:
        params["manufacturer"] = manufacturer

    try:
        result = await do_get_product(admin_state.engine, params)
        return JSONResponse({**result, "status": "ok"})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Product get error",
            exc,
            status_code=500,
            log_event="api_product_get",
        )


# ---------------------------------------------------------------------------
# Forbidden columns — never leak to callers (ADR-0017)
# ---------------------------------------------------------------------------
_REVIEW_HIDDEN: frozenset[str] = frozenset({"cost", "cost_price", "margin", "bid_id"})


async def api_product_enrichment_review(request) -> JSONResponse:
    """GET /api/product/enrichment/review

    Returns ``needs_review=true`` rows from ``product_enrichment_log`` for the
    caller's namespace.  Namespace-scoped via FORCE RLS — callers cannot see
    another tenant's rows.  Never returns cost, margin, or BID columns.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        limit        (int, optional): Maximum rows to return (default 50, max 200).

    Response (JSON):
        {"status": "ok", "items": [...], "total": <int>}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = request.query_params.get("namespace_id", "").strip()
    raw_limit = request.query_params.get("limit", "50")

    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    # See api_product_search: explicit UUID check must precede the opt-in
    # gate — validate_agent_id() never raises, so it cannot do this job.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    disabled = await _check_product_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        limit = max(1, min(200, int(raw_limit)))
    except (TypeError, ValueError):
        limit = 50

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
            rows = await conn.fetch(
                """
                SELECT id, namespace_id, product_id, trigger_context,
                       field_name, field_value, confidence,
                       needs_review, product_source_id, created_at
                FROM   product_enrichment_log
                WHERE  needs_review = true
                ORDER  BY created_at DESC
                LIMIT  $1
                """,
                limit,
            )
        items = [
            {k: v for k, v in serialize_pg_row(row).items() if k not in _REVIEW_HIDDEN}
            for row in rows
        ]
        return JSONResponse({"status": "ok", "items": items, "total": len(items)})
    except Exception as exc:
        return admin_error_response(
            "Product enrichment review error",
            exc,
            status_code=500,
            log_event="api_product_enrichment_review",
        )
