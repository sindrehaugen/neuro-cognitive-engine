"""
Admin HTTP handlers for the Vendors vertical module (M4.W3).

Exports:
  ``api_vendors_get_vendor``   — GET  /api/vendors/{id}
  ``api_vendors_scorecard``    — GET  /api/vendors/scorecard
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
)
from nce.auth import validate_agent_id
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors import do_get_vendor

log = logging.getLogger("nce.admin_handlers.vendors")


async def api_vendors_get_vendor(request: Any) -> JSONResponse:
    """GET /api/vendors/{id}

    Path parameter:
        id (str): vendor_id (label or ID or vendors_source_id).

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    vendor_id = request.path_params.get("id", "").strip()
    namespace_id = request.query_params.get("namespace_id", "").strip()

    if not vendor_id:
        return JSONResponse({"error": "Path param 'id' is required"}, status_code=422)
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    try:
        result = await do_get_vendor(
            admin_state.engine,
            {"namespace_id": namespace_id, "vendor_id": vendor_id},
        )
        if result is None:
            return JSONResponse({"status": "ok", "vendor": None}, status_code=404)
        return JSONResponse({"status": "ok", "vendor": result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Vendors get_vendor error",
            exc,
            status_code=500,
            log_event="api_vendors_get_vendor",
        )


async def api_vendors_scorecard(request: Any) -> JSONResponse:
    """GET /api/vendors/scorecard

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        vendor_id (str, optional): vendor_id to filter/return dashboard for.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = request.query_params.get("namespace_id", "").strip()
    vendor_id = request.query_params.get("vendor_id", "").strip()

    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    try:
        if vendor_id:
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                row = await conn.fetchrow(
                    """
                    SELECT vendor_id, on_time_pct, defect_rma_rate, substitution_rate,
                           reliability, current_tier, ytd_progress, sample_n, computed_at
                    FROM vendor_scorecards
                    WHERE vendor_id = $1 AND namespace_id = $2
                    """,
                    vendor_id,
                    namespace_id,
                )
                result = [dict(row)] if row else []
        else:
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                rows = await conn.fetch(
                    """
                    SELECT vendor_id, on_time_pct, defect_rma_rate, substitution_rate,
                           reliability, current_tier, ytd_progress, sample_n, computed_at
                    FROM vendor_scorecards
                    WHERE namespace_id = $1
                    ORDER BY vendor_id
                    """,
                    namespace_id,
                )
                result = [dict(r) for r in rows]

        # Convert Decimal values to float for JSON compatibility
        for r in result:
            for k in [
                "on_time_pct",
                "defect_rma_rate",
                "substitution_rate",
                "reliability",
                "ytd_progress",
            ]:
                if r.get(k) is not None:
                    r[k] = float(r[k])
            if r.get("computed_at"):
                r["computed_at"] = str(r["computed_at"])

        return JSONResponse({"status": "ok", "scorecards": result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Vendors scorecard error",
            exc,
            status_code=500,
            log_event="api_vendors_scorecard",
        )
