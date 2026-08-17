"""
Admin HTTP handlers for the Sales vertical module (C5 source-mode-divergence + read-routes).

Exports:
  ``api_sales_source_mode_get`` — GET  /api/admin/sales/source-mode
  ``api_sales_source_mode_put`` — PUT  /api/admin/sales/source-mode
  ``api_admin_sales_customers`` — GET  /api/sales/customers
  ``api_admin_sales_customer_profile`` — GET  /api/sales/customers/{id}
  ``api_admin_sales_overview`` — GET  /api/sales/overview
  ``api_admin_sales_seller_detail`` — GET  /api/sales/seller-detail/{user}
  ``api_admin_sales_dashboard`` — GET  /api/sales/dashboard
  ``api_admin_sales_stats`` — GET  /api/sales/stats
  ``api_admin_sales_manager`` — GET  /api/sales/manager
  ``api_admin_sales_agreements`` — GET  /api/sales/agreements
  ``api_admin_sales_agreement_detail`` — GET  /api/sales/agreements/{id}
  ``api_admin_sales_quote_detail`` — GET  /api/sales/quotes/{id}
  ``api_admin_sales_targets_get`` — GET  /api/sales/targets
  ``api_admin_sales_targets_put`` — PUT  /api/sales/targets
"""

from __future__ import annotations

import logging
import uuid

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
)
from nce.db_utils import scoped_pg_session
from nce.source_mode.divergence import flip_blocked
from nce.vertical_modules.sales.read_model import (
    do_get_targets,
    do_set_target,
)
from nce.vertical_modules.sales.source_mode import (
    do_agreement_detail,
    do_customer_profile,
    do_list_agreements,
    do_list_customers,
    do_quote_detail,
    do_sales_dashboard,
    do_sales_manager,
    do_sales_overview,
    do_sales_stats,
    do_seller_detail,
)

log = logging.getLogger("nce.admin_handlers.sales")


def _validate_namespace_query_param(namespace_id: str) -> JSONResponse | None:
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )
    try:
        uuid.UUID(namespace_id)
        return None
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)


# ---------------------------------------------------------------------------
# GET /api/admin/sales/source-mode
# ---------------------------------------------------------------------------


async def api_sales_source_mode_get(request) -> JSONResponse:
    """GET /api/admin/sales/source-mode

    Retrieve the configured source modes for engine="sales".

    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {
          "namespace_id": str,
          "engine": "sales",
          "modes": {
            "list_customers": "d365" | "both" | "nce",
            ...
          }
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    try:
        ns_uuid = uuid.UUID(namespace_id)
        async with scoped_pg_session(admin_state.engine.pg_pool, ns_uuid) as conn:
            rows = await conn.fetch(
                """
                SELECT function, mode
                  FROM source_mode_config
                 WHERE namespace_id = $1
                   AND engine = $2
                """,
                ns_uuid,
                "sales",
            )
        modes = {row["function"]: row["mode"] for row in rows}
        return JSONResponse(
            {
                "namespace_id": namespace_id,
                "engine": "sales",
                "modes": modes,
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Sales source-mode GET error",
            exc,
            status_code=500,
            log_event="api_sales_source_mode_get",
        )


# ---------------------------------------------------------------------------
# PUT /api/admin/sales/source-mode
# ---------------------------------------------------------------------------


async def api_sales_source_mode_put(request) -> JSONResponse:
    """PUT /api/admin/sales/source-mode

    Configure the source mode for a given sales function.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        function     (str, required): Function key (e.g. "list_customers").
        mode         (str, required): Target mode ("d365", "both", or "nce").

    Response (JSON):
        {
          "namespace_id": str,
          "engine": "sales",
          "function": str,
          "mode": str,
          "status": "updated"
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id = str(body.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    try:
        uuid.UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    func_name = str(body.get("function") or "").strip()
    if not func_name:
        return JSONResponse({"error": "Missing required field: function"}, status_code=422)

    valid_functions = {
        "list_customers",
        "customer_profile",
        "sales_overview",
        "seller_detail",
        "sales_dashboard",
        "sales_stats",
        "sales_manager",
        "list_agreements",
        "agreement_detail",
        "quote_detail",
    }
    if func_name not in valid_functions:
        return JSONResponse(
            {"error": f"Invalid function: {func_name}. Must be one of {sorted(valid_functions)}"},
            status_code=422,
        )

    mode = str(body.get("mode") or "").strip()
    if mode not in ("d365", "both", "nce"):
        return JSONResponse(
            {"error": f"Invalid mode: {mode}. Must be one of ('d365', 'both', 'nce')"},
            status_code=422,
        )

    ns_uuid = uuid.UUID(namespace_id)

    if mode == "nce":
        try:
            blocked = await flip_blocked(
                admin_state.engine.pg_pool,
                namespace_id=ns_uuid,
                engine="sales",
                window_seconds=3600.0,
            )
            if blocked:
                return JSONResponse(
                    {"error": "Flip to nce mode is blocked due to recent divergences"},
                    status_code=400,
                )
        except Exception as exc:
            return admin_error_response(
                "Sales source-mode check flip-blocked error",
                exc,
                status_code=500,
                log_event="api_sales_source_mode_put_check",
            )

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, ns_uuid) as conn:
            await conn.execute(
                """
                INSERT INTO source_mode_config (namespace_id, engine, function, mode, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (namespace_id, engine, function)
                DO UPDATE SET mode = EXCLUDED.mode, updated_at = EXCLUDED.updated_at
                """,
                ns_uuid,
                "sales",
                func_name,
                mode,
            )
        return JSONResponse(
            {
                "namespace_id": namespace_id,
                "engine": "sales",
                "function": func_name,
                "mode": mode,
                "status": "updated",
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Sales source-mode PUT error",
            exc,
            status_code=500,
            log_event="api_sales_source_mode_put",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/customers
# ---------------------------------------------------------------------------


async def api_admin_sales_customers(request) -> JSONResponse:
    """GET /api/sales/customers

    List all customers for the active namespace.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "q": request.query_params.get("q", ""),
        "size": int(request.query_params.get("size") or 100),
        "page": int(request.query_params.get("page") or 0),
        "include_deleted": request.query_params.get("include_deleted", "false").lower() == "true",
    }

    try:
        result = await do_list_customers(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales list-customers error",
            exc,
            status_code=500,
            log_event="api_admin_sales_customers",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/customers/{id}
# ---------------------------------------------------------------------------


async def api_admin_sales_customer_profile(request) -> JSONResponse:
    """GET /api/sales/customers/{id}

    Retrieve detailed customer profile.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    customer_id = request.path_params.get("id", "").strip()
    if not customer_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "accountid": customer_id,
    }

    try:
        result = await do_customer_profile(admin_state.engine, params)
        if "error" in result:
            if result.get("error") == "unknown_company":
                return JSONResponse(result, status_code=404)
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales customer-profile error",
            exc,
            status_code=500,
            log_event="api_admin_sales_customer_profile",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/overview
# ---------------------------------------------------------------------------


async def api_admin_sales_overview(request) -> JSONResponse:
    """GET /api/sales/overview

    Retrieve aggregated sales pipeline overview stages.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
    }

    try:
        result = await do_sales_overview(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales overview error",
            exc,
            status_code=500,
            log_event="api_admin_sales_overview",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/seller-detail/{user}
# ---------------------------------------------------------------------------


async def api_admin_sales_seller_detail(request) -> JSONResponse:
    """GET /api/sales/seller-detail/{user}

    Retrieve active pipeline details for a seller.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    user = request.path_params.get("user", "").strip()
    if not user:
        return JSONResponse({"error": "Missing path parameter: user"}, status_code=422)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "user": user,
    }

    try:
        result = await do_seller_detail(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales seller-detail error",
            exc,
            status_code=500,
            log_event="api_admin_sales_seller_detail",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/dashboard
# ---------------------------------------------------------------------------


async def api_admin_sales_dashboard(request) -> JSONResponse:
    """GET /api/sales/dashboard

    Retrieve sales dashboard data for team or a specific owner.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    team_query = request.query_params.get("team", "false").lower() == "true"
    owner_query = str(request.query_params.get("owner") or "").strip()

    params = {
        "namespace_id": namespace_id,
        "user": "admin" if team_query else owner_query,
    }
    today_query = request.query_params.get("today")
    if today_query:
        params["today"] = today_query

    try:
        result = await do_sales_dashboard(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales dashboard error",
            exc,
            status_code=500,
            log_event="api_admin_sales_dashboard",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/stats
# ---------------------------------------------------------------------------


async def api_admin_sales_stats(request) -> JSONResponse:
    """GET /api/sales/stats

    Retrieve sales statistics segmented by IT/AV and sector.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "period": request.query_params.get("period", "month"),
        "offset": int(request.query_params.get("offset") or 0),
        "today": request.query_params.get("today"),
    }

    try:
        result = await do_sales_stats(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales stats error",
            exc,
            status_code=500,
            log_event="api_admin_sales_stats",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/manager
# ---------------------------------------------------------------------------


async def api_admin_sales_manager(request) -> JSONResponse:
    """GET /api/sales/manager

    Retrieve manager-level sales team performance dashboard.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "period": request.query_params.get("period", "month"),
        "offset": int(request.query_params.get("offset") or 0),
        "today": request.query_params.get("today"),
    }

    try:
        result = await do_sales_manager(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales manager error",
            exc,
            status_code=500,
            log_event="api_admin_sales_manager",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/agreements
# ---------------------------------------------------------------------------


async def api_admin_sales_agreements(request) -> JSONResponse:
    """GET /api/sales/agreements

    List all agreements for the active namespace.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "q": request.query_params.get("q", ""),
        "size": int(request.query_params.get("size") or 100),
        "page": int(request.query_params.get("page") or 0),
        "include_deleted": request.query_params.get("include_deleted", "false").lower() == "true",
    }

    try:
        result = await do_list_agreements(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales agreements error",
            exc,
            status_code=500,
            log_event="api_admin_sales_agreements",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/agreements/{id}
# ---------------------------------------------------------------------------


async def api_admin_sales_agreement_detail(request) -> JSONResponse:
    """GET /api/sales/agreements/{id}

    Retrieve detailed agreement record.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    agreement_id = request.path_params.get("id", "").strip()
    if not agreement_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "agreementid": agreement_id,
    }

    try:
        result = await do_agreement_detail(admin_state.engine, params)
        if "error" in result:
            if result.get("error") == "unknown_agreement":
                return JSONResponse(result, status_code=404)
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales agreement-detail error",
            exc,
            status_code=500,
            log_event="api_admin_sales_agreement_detail",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/quotes/{id}
# ---------------------------------------------------------------------------


async def api_admin_sales_quote_detail(request) -> JSONResponse:
    """GET /api/sales/quotes/{id}

    Retrieve detailed quote record.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    quote_id = request.path_params.get("id", "").strip()
    if not quote_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
        "quoteid": quote_id,
    }

    try:
        result = await do_quote_detail(admin_state.engine, params)
        if "error" in result:
            if result.get("error") == "unknown_quote":
                return JSONResponse(result, status_code=404)
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales quote-detail error",
            exc,
            status_code=500,
            log_event="api_admin_sales_quote_detail",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/targets
# ---------------------------------------------------------------------------


async def api_admin_sales_targets_get(request) -> JSONResponse:
    """GET /api/sales/targets

    Retrieve configured monthly targets for all sellers in the namespace.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params = {
        "namespace_id": namespace_id,
    }

    try:
        result = await do_get_targets(admin_state.engine, params)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Sales targets get error",
            exc,
            status_code=500,
            log_event="api_admin_sales_targets_get",
        )


# ---------------------------------------------------------------------------
# PUT /api/sales/targets
# ---------------------------------------------------------------------------


async def api_admin_sales_targets_put(request) -> JSONResponse:
    """PUT /api/sales/targets

    Configure monthly targets (meetings or sales value) for a seller.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id = str(body.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    try:
        uuid.UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    owner_slug = body.get("owner_slug") or body.get("owner")
    metric = body.get("metric")
    value = body.get("value")

    if not owner_slug or metric not in ("meetings_monthly", "won_monthly"):
        return JSONResponse(
            {"error": "owner_slug and valid metric (meetings_monthly/won_monthly) are required"},
            status_code=422,
        )

    params = {
        "namespace_id": namespace_id,
        "owner_slug": owner_slug,
        "metric": metric,
        "value": value,
    }

    try:
        result = await do_set_target(admin_state.engine, params)
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales targets set error",
            exc,
            status_code=500,
            log_event="api_admin_sales_targets_put",
        )
