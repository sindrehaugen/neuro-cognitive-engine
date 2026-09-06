"""
Admin HTTP handlers for the Vendors vertical module (M4.W3 & Wave V-1).

Exports:
  ``api_vendors_get_vendor``          — GET  /api/vendors/{id}
  ``api_vendors_scorecard``           — GET  /api/vendors/scorecard
  ``api_vendors_upsert``              — POST /api/vendors/upsert
  ``api_vendors_upsert_contractor``   — POST /api/vendors/contractors/upsert
  ``api_vendors_get_contractor``      — GET  /api/vendors/contractors/{id}
  ``api_vendors_upsert_cert``         — POST /api/vendors/certs/upsert
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
)
from nce.auth import validate_agent_id
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors import (
    do_get_contractor,
    do_get_vendor,
    do_upsert_cert,
    do_upsert_contractor,
    do_upsert_vendor,
)

log = logging.getLogger("nce.admin_handlers.vendors")


async def _extract_request_data(request: Any) -> dict[str, Any]:
    if hasattr(request, "json") and callable(request.json):
        try:
            body = await request.json()
            if isinstance(body, dict):
                return dict(body)
        except Exception:
            pass
    if hasattr(request, "query_params"):
        return dict(request.query_params)
    return {}


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


async def api_vendors_upsert(request: Any) -> JSONResponse:
    """POST /api/vendors/upsert

    Body parameters:
        namespace_id (str, required): Active namespace UUID.
        orgnr (str, required): Organization registration number.
        name (str, required): Vendor organization name.
        feed_fields (dict, optional): External feed attributes.
        admin_fields (dict, optional): Admin-entered attributes.
        source_id (str, optional): Source identifier.
        source_type (str, optional): 'feed' or 'admin'.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    data = await _extract_request_data(request)
    namespace_id = data.get("namespace_id", "")
    if isinstance(namespace_id, str):
        namespace_id = namespace_id.strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    namespace_id = validate_agent_id(str(namespace_id))
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    orgnr = data.get("orgnr")
    if not orgnr:
        return JSONResponse({"error": "Missing required field: orgnr"}, status_code=422)
    name = data.get("name")
    if not name:
        return JSONResponse({"error": "Missing required field: name"}, status_code=422)

    params = dict(data)
    params["namespace_id"] = namespace_id

    try:
        result = await do_upsert_vendor(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine, route="api_vendors_upsert")
        return JSONResponse({"status": "ok", "result": result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Vendors upsert error",
            exc,
            status_code=500,
            log_event="api_vendors_upsert",
        )


async def api_vendors_upsert_contractor(request: Any) -> JSONResponse:
    """POST /api/vendors/contractors/upsert

    Body parameters:
        namespace_id (str, required): Active namespace UUID.
        contractor_id (str, required): Contractor identifier or label.
        partner_scope_id (str, required): Partner scope UUID.
        profile (dict, optional): Contractor profile metadata.
        rates (dict, optional): Billing rates metadata.
        skills (list[str], optional): Skills list.
        availability (dict, optional): Availability metadata.
        performance_score (float, optional): Performance score.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    data = await _extract_request_data(request)
    namespace_id = data.get("namespace_id", "")
    if isinstance(namespace_id, str):
        namespace_id = namespace_id.strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    namespace_id = validate_agent_id(str(namespace_id))
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    contractor_id = data.get("contractor_id")
    if not contractor_id:
        return JSONResponse({"error": "Missing required field: contractor_id"}, status_code=422)

    partner_scope_id = data.get("partner_scope_id")
    if not partner_scope_id:
        return JSONResponse({"error": "Missing required field: partner_scope_id"}, status_code=422)

    try:
        UUID(str(partner_scope_id))
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid partner_scope_id: {exc}"}, status_code=422)

    params = dict(data)
    params["namespace_id"] = namespace_id

    try:
        result = await do_upsert_contractor(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine, route="api_vendors_upsert_contractor")
        return JSONResponse({"status": "ok", "result": result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Vendors upsert_contractor error",
            exc,
            status_code=500,
            log_event="api_vendors_upsert_contractor",
        )


async def api_vendors_get_contractor(request: Any) -> JSONResponse:
    """GET /api/vendors/contractors/{id}

    Path parameter:
        id (str): contractor_id (label or ID).

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        partner_scope_id (str, optional): Partner scope UUID for RLS context.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    contractor_id = request.path_params.get("id", "").strip()
    namespace_id = request.query_params.get("namespace_id", "").strip()
    partner_scope_id = request.query_params.get("partner_scope_id", "").strip() or None

    if not contractor_id:
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

    if partner_scope_id:
        try:
            UUID(partner_scope_id)
        except ValueError as exc:
            return JSONResponse({"error": f"Invalid partner_scope_id: {exc}"}, status_code=422)

    try:
        result = await do_get_contractor(
            admin_state.engine,
            {
                "namespace_id": namespace_id,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope_id,
            },
        )
        if result is None:
            return JSONResponse({"status": "ok", "contractor": None}, status_code=404)
        return JSONResponse({"status": "ok", "contractor": result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Vendors get_contractor error",
            exc,
            status_code=500,
            log_event="api_vendors_get_contractor",
        )


async def api_vendors_upsert_cert(request: Any) -> JSONResponse:
    """POST /api/vendors/certs/upsert

    Body parameters:
        namespace_id (str, required): Active namespace UUID.
        contractor_id (str, required): Contractor identifier or label.
        cert_name (str, required): Certification identifier or name.
        expiry_date (str, required): Expiration date (YYYY-MM-DD).
        name (str, optional): Friendly name.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    data = await _extract_request_data(request)
    namespace_id = data.get("namespace_id", "")
    if isinstance(namespace_id, str):
        namespace_id = namespace_id.strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    namespace_id = validate_agent_id(str(namespace_id))
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    contractor_id = data.get("contractor_id")
    if not contractor_id:
        return JSONResponse({"error": "Missing required field: contractor_id"}, status_code=422)

    cert_name = data.get("cert_name")
    if not cert_name:
        return JSONResponse({"error": "Missing required field: cert_name"}, status_code=422)

    expiry_date = data.get("expiry_date")
    if not expiry_date:
        return JSONResponse({"error": "Missing required field: expiry_date"}, status_code=422)

    params = dict(data)
    params["namespace_id"] = namespace_id

    try:
        result = await do_upsert_cert(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine, route="api_vendors_upsert_cert")
        return JSONResponse({"status": "ok", "result": result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Vendors upsert_cert error",
            exc,
            status_code=500,
            log_event="api_vendors_upsert_cert",
        )
