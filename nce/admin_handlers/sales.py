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
  ``api_admin_sales_calculate_commission`` — GET  /api/sales/commission
  ``api_admin_sales_divergences`` — GET  /api/sales/divergences
  ``api_admin_sales_morning_brief_slice`` — GET  /api/sales/morning-brief
  ``api_admin_sales_lead_score`` — POST /api/sales/lead-score
  ``api_admin_sales_quote_draft`` — POST /api/sales/quote-draft
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
)
from nce.db_utils import scoped_pg_session
from nce.source_mode.divergence import flip_blocked
from nce.vertical_modules.sales.ai import do_draft_quote, do_score_lead
from nce.vertical_modules.sales.commission import do_calculate_commission
from nce.vertical_modules.sales.flip import (
    do_morning_brief_slice,
    do_read_sales_divergence,
)
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
from nce.vertical_modules.sales.write_routing import (
    do_create_customer,
    do_create_deal,
    do_create_lead,
    do_edit_deal,
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


# ---------------------------------------------------------------------------
# POST /api/sales/customers
# ---------------------------------------------------------------------------


async def api_admin_sales_create_customer(request) -> JSONResponse:
    """POST /api/sales/customers

    Create a customer account through C5 write routing.
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

    customer_id = body.get("customer_id")
    if not customer_id or not isinstance(customer_id, str) or not customer_id.strip():
        return JSONResponse({"error": "Missing required field: customer_id"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "customer_id": customer_id.strip(),
    }
    if "name" in body and body["name"] is not None:
        params["name"] = str(body["name"]).strip()
    if "source_id" in body and body["source_id"] is not None:
        params["source_id"] = str(body["source_id"]).strip()

    try:
        result = await do_create_customer(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine, route="api_admin_sales_create_customer")
        return JSONResponse(result, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales create customer error",
            exc,
            status_code=500,
            log_event="api_admin_sales_create_customer",
        )


# ---------------------------------------------------------------------------
# POST /api/sales/leads
# ---------------------------------------------------------------------------


async def api_admin_sales_create_lead(request) -> JSONResponse:
    """POST /api/sales/leads

    Create a sales lead through C5 write routing.
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

    lead_id = body.get("lead_id")
    if not lead_id or not isinstance(lead_id, str) or not lead_id.strip():
        return JSONResponse({"error": "Missing required field: lead_id"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "lead_id": lead_id.strip(),
    }
    if "customer_id" in body and body["customer_id"] is not None:
        params["customer_id"] = str(body["customer_id"]).strip()
    if "name" in body and body["name"] is not None:
        params["name"] = str(body["name"]).strip()
    if "confidence" in body and body["confidence"] is not None:
        params["confidence"] = float(body["confidence"])
    if "source_id" in body and body["source_id"] is not None:
        params["source_id"] = str(body["source_id"]).strip()

    try:
        result = await do_create_lead(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine, route="api_admin_sales_create_lead")
        return JSONResponse(result, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales create lead error",
            exc,
            status_code=500,
            log_event="api_admin_sales_create_lead",
        )


# ---------------------------------------------------------------------------
# POST /api/sales/deals
# ---------------------------------------------------------------------------


async def api_admin_sales_create_deal(request) -> JSONResponse:
    """POST /api/sales/deals

    Create a pipeline deal through C5 write routing.
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

    deal_id = body.get("deal_id")
    customer_id = body.get("customer_id")
    quote_id = body.get("quote_id")

    if not (deal_id and isinstance(deal_id, str) and deal_id.strip()):
        return JSONResponse({"error": "Missing required field: deal_id"}, status_code=422)
    if not (customer_id and isinstance(customer_id, str) and customer_id.strip()):
        return JSONResponse({"error": "Missing required field: customer_id"}, status_code=422)
    if not (quote_id and isinstance(quote_id, str) and quote_id.strip()):
        return JSONResponse({"error": "Missing required field: quote_id"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "deal_id": deal_id.strip(),
        "customer_id": customer_id.strip(),
        "quote_id": quote_id.strip(),
    }
    if "opportunity_id" in body and body["opportunity_id"] is not None:
        params["opportunity_id"] = str(body["opportunity_id"]).strip()
    if "lead_id" in body and body["lead_id"] is not None:
        params["lead_id"] = str(body["lead_id"]).strip()
    if "name" in body and body["name"] is not None:
        params["name"] = str(body["name"]).strip()
    if "confidence" in body and body["confidence"] is not None:
        params["confidence"] = float(body["confidence"])
    if "source_id" in body and body["source_id"] is not None:
        params["source_id"] = str(body["source_id"]).strip()

    try:
        result = await do_create_deal(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine, route="api_admin_sales_create_deal")
        return JSONResponse(result, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales create deal error",
            exc,
            status_code=500,
            log_event="api_admin_sales_create_deal",
        )


# ---------------------------------------------------------------------------
# POST /api/sales/deals/edit
# ---------------------------------------------------------------------------


async def api_admin_sales_edit_deal(request) -> JSONResponse:
    """POST /api/sales/deals/edit

    Edit a pipeline deal through C5 write routing.
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

    deal_id = body.get("deal_id")
    if not deal_id or not isinstance(deal_id, str) or not deal_id.strip():
        return JSONResponse({"error": "Missing required field: deal_id"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "deal_id": deal_id.strip(),
    }
    if "name" in body and body["name"] is not None:
        params["name"] = str(body["name"]).strip()
    if "confidence" in body and body["confidence"] is not None:
        params["confidence"] = float(body["confidence"])
    if "source_id" in body and body["source_id"] is not None:
        params["source_id"] = str(body["source_id"]).strip()

    try:
        result = await do_edit_deal(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine, route="api_admin_sales_edit_deal")
        return JSONResponse(result, status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales edit deal error",
            exc,
            status_code=500,
            log_event="api_admin_sales_edit_deal",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/commission
# ---------------------------------------------------------------------------


async def api_admin_sales_calculate_commission(request) -> JSONResponse:
    """GET /api/sales/commission

    Calculate reproducible DB-weighted sales commissions from ledger events or deal items.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        seller_id    (str, optional): Seller ID to filter historical deals won.

    Response (JSON):
        {
          "ok": True,
          "seller_id": str | None,
          "total_commission": float,
          "commissions": list[dict],
          "config_version": str,
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params: dict[str, Any] = {"namespace_id": namespace_id}
    seller_id = request.query_params.get("seller_id")
    if seller_id is not None and str(seller_id).strip():
        params["seller_id"] = str(seller_id).strip()

    try:
        result = await do_calculate_commission(admin_state.engine, params)
        return JSONResponse(result, status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales calculate commission error",
            exc,
            status_code=500,
            log_event="api_admin_sales_calculate_commission",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/divergences
# ---------------------------------------------------------------------------


async def api_admin_sales_divergences(request) -> JSONResponse:
    """GET /api/sales/divergences

    Read Sales divergence log entries and evaluate the parity window.

    Query parameters:
        namespace_id   (str, required): Active namespace UUID.
        window_days    (float, optional): Window size in days (default 7.0).
        window_seconds (float, optional): Window size in seconds (overrides window_days).
        entity         (str, optional): Entity filter (e.g. "accounts", "opportunities").
        limit          (int, optional): Max items (default 100, max 500).
        offset         (int, optional): Pagination offset (default 0).

    Response (JSON):
        {
          "ok": True,
          "namespace_id": str,
          "engine": "sales",
          "window_seconds": float,
          "clean": bool,
          "flip_blocked": bool,
          "divergences_count": int,
          "material_divergences_count": int,
          "alert_threshold": float,
          "items": list[dict],
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params: dict[str, Any] = {"namespace_id": namespace_id}
    if "window_days" in request.query_params:
        try:
            params["window_days"] = float(request.query_params["window_days"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "window_days must be a number"}, status_code=422)
    if "window_seconds" in request.query_params:
        try:
            params["window_seconds"] = float(request.query_params["window_seconds"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "window_seconds must be a number"}, status_code=422)
    if "entity" in request.query_params and request.query_params["entity"].strip():
        params["entity"] = request.query_params["entity"].strip()
    if "limit" in request.query_params:
        try:
            params["limit"] = int(request.query_params["limit"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "limit must be an integer"}, status_code=422)
    if "offset" in request.query_params:
        try:
            params["offset"] = int(request.query_params["offset"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "offset must be an integer"}, status_code=422)

    try:
        result = await do_read_sales_divergence(admin_state.engine, params)
        return JSONResponse(result, status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales divergence log error",
            exc,
            status_code=500,
            log_event="api_admin_sales_divergences",
        )


# ---------------------------------------------------------------------------
# GET /api/sales/morning-brief
# ---------------------------------------------------------------------------


async def api_admin_sales_morning_brief_slice(request) -> JSONResponse:
    """GET /api/sales/morning-brief

    Expose executive morning brief slice for the Sales vertical.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        period_days  (int, optional): Lookback period in days for won deals (default 7).

    Response (JSON):
        {
          "ok": True,
          "pipeline_value": float,
          "at_risk_deals_count": int,
          "won_value_this_period": float,
          "won_count_this_period": int,
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if err_resp := _validate_namespace_query_param(namespace_id):
        return err_resp

    params: dict[str, Any] = {"namespace_id": namespace_id}
    if "period_days" in request.query_params:
        try:
            params["period_days"] = int(request.query_params["period_days"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "period_days must be an integer"}, status_code=422)

    try:
        result = await do_morning_brief_slice(admin_state.engine, params)
        return JSONResponse(result, status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales morning brief slice error",
            exc,
            status_code=500,
            log_event="api_admin_sales_morning_brief_slice",
        )


# ---------------------------------------------------------------------------
# POST /api/sales/lead-score
# ---------------------------------------------------------------------------


async def api_admin_sales_lead_score(request) -> JSONResponse:
    """POST /api/sales/lead-score

    Calculate lead score and confidence from similar historical deals (Advisor).

    Body parameters:
        namespace_id (str, required): Active namespace UUID.
        lead_name    (str, optional): Lead name.
        query_text   (str, optional): Freeform query text describing the lead.
        subject      (str, optional): Lead subject.

    Response (JSON):
        {
          "ok": True,
          "score": float,
          "confidence": float,
          "propose_only": True,
          "reasons": list[str],
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    namespace_id = str(
        body.get("namespace_id") or request.query_params.get("namespace_id") or ""
    ).strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    try:
        uuid.UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    params: dict[str, Any] = {"namespace_id": namespace_id}
    if "lead_name" in body and body["lead_name"] is not None:
        params["lead_name"] = str(body["lead_name"]).strip()
    elif "lead_name" in request.query_params:
        params["lead_name"] = str(request.query_params["lead_name"]).strip()

    if "query_text" in body and body["query_text"] is not None:
        params["query_text"] = str(body["query_text"]).strip()
    elif "query_text" in request.query_params:
        params["query_text"] = str(request.query_params["query_text"]).strip()

    if "subject" in body and body["subject"] is not None:
        params["subject"] = str(body["subject"]).strip()
    elif "subject" in request.query_params:
        params["subject"] = str(request.query_params["subject"]).strip()

    try:
        result = await do_score_lead(admin_state.engine, params)
        return JSONResponse(result, status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales lead score error",
            exc,
            status_code=500,
            log_event="api_admin_sales_lead_score",
        )


# ---------------------------------------------------------------------------
# POST /api/sales/quote-draft
# ---------------------------------------------------------------------------


async def api_admin_sales_quote_draft(request) -> JSONResponse:
    """POST /api/sales/quote-draft

    AI Quote-Draft Assist (Advisor).

    Body parameters:
        namespace_id   (str, required): Active namespace UUID.
        opportunity_id (str, optional): Opportunity identifier.
        description    (str, optional): Quote description / requirements.

    Response (JSON):
        {
          "ok": True,
          "proposed_lines": list[dict],
          "suggested_margin_pct": float,
          "propose_only": True,
          "validated": False,
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    namespace_id = str(
        body.get("namespace_id") or request.query_params.get("namespace_id") or ""
    ).strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    try:
        uuid.UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    params: dict[str, Any] = {"namespace_id": namespace_id}
    if "opportunity_id" in body and body["opportunity_id"] is not None:
        params["opportunity_id"] = str(body["opportunity_id"]).strip()
    elif "opportunity_id" in request.query_params:
        params["opportunity_id"] = str(request.query_params["opportunity_id"]).strip()

    if "description" in body and body["description"] is not None:
        params["description"] = str(body["description"]).strip()
    elif "description" in request.query_params:
        params["description"] = str(request.query_params["description"]).strip()

    try:
        result = await do_draft_quote(admin_state.engine, params)
        return JSONResponse(result, status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Sales quote draft error",
            exc,
            status_code=500,
            log_event="api_admin_sales_quote_draft",
        )
