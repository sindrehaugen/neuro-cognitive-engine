"""
Admin HTTP handler for pricing operations (Wave 13).

Exports api_pricing_resolve for wiring into build_admin_routes.
"""

from __future__ import annotations

import json

from nce.admin_handlers._shared import JSONResponse, admin_error_response, admin_state
from nce.auth import validate_agent_id
from nce.db_utils import scoped_pg_session
from nce.pricing.resolver import resolve_price


async def api_pricing_resolve(request) -> JSONResponse:
    """POST /api/admin/pricing/resolve — resolve pricing for a (product, customer) pair.

    Request body (JSON):
        namespace_id (str):        Required. Namespace UUID.
        product (dict, optional):  Product pricing data.
        customer (dict, optional): Customer pricing data.

    Response (JSON):
        {
            "status": "ok",
            "cost": float,
            "source": "bid" | "supplier_list" | "base",
            "as_of": ISO 8601 datetime,
            "stale": bool,         # Never silently dropped
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    namespace_id = body.get("namespace_id")
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required field: namespace_id"},
            status_code=422,
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as e:
        return JSONResponse(
            {"error": f"Invalid namespace_id: {str(e)}"},
            status_code=422,
        )

    product = dict(body.get("product") or {})
    customer = dict(body.get("customer") or {})

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
            result = await resolve_price(
                conn,
                namespace_id=namespace_id,
                product=product,
                customer=customer,
            )

        return JSONResponse(
            {
                "status": "ok",
                "cost": result["cost"],
                "source": result["source"],
                "as_of": result["as_of"].isoformat(),
                "stale": result["stale"],
            }
        )
    except ValueError as e:
        return JSONResponse(
            {"error": f"Pricing resolution failed: {str(e)}"},
            status_code=422,
        )
    except Exception as e:
        return admin_error_response(
            "Pricing resolution error",
            e,
            status_code=500,
            log_event="api_pricing_resolve",
        )
