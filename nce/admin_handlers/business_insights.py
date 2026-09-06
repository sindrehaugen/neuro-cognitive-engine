"""
nce/admin_handlers/business_insights.py
=======================================
Admin HTTP handlers for Module 16: Business Insights Engine (ML16-B6).

Exports:
  api_business_insights_morning_brief       — GET  /api/business-insights/morning-brief
  api_business_insights_risk_radar           — GET  /api/business-insights/risk-radar
  api_business_insights_run_scenario         — POST /api/business-insights/run-scenario
  api_business_insights_board_pack           — GET/POST /api/business-insights/board-pack
  api_business_insights_kpi_dashboard        — GET  /api/business-insights/kpi-dashboard
  api_business_insights_ask                 — POST /api/business-insights/ask

All exception handlers strictly pass (message, exc) to admin_error_response.
All routes enforce tenant isolation and require metadata.business_insights.enabled = true.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.admin_handlers._shared import (
    _MISSING_NAMESPACE_QUERY_PARAM,
    JSONResponse,
    _json_safe,
    _require_namespace_id,
    admin_error_response,
    admin_state,
)
from nce.vertical_modules.business_insights._guard import (
    BusinessInsightsDisabledError,
    PersonRankingProhibitedError,
    ThirdPartyEgressUnauthorizedError,
    require_business_insights_enabled,
)
from nce.vertical_modules.business_insights.ask import do_ask_business
from nce.vertical_modules.business_insights.board_pack import do_generate_board_pack
from nce.vertical_modules.business_insights.brief import do_morning_brief
from nce.vertical_modules.business_insights.kpi import do_kpi_dashboard
from nce.vertical_modules.business_insights.radar import do_risk_radar
from nce.vertical_modules.business_insights.scenario import do_run_scenario

log = logging.getLogger("nce.admin_handlers.business_insights")


def _extract_pool(engine: Any) -> Any:
    """Extract an asyncpg pool from the engine context."""
    if hasattr(engine, "pg_pool") and (
        "pg_pool" in getattr(engine, "__dict__", {}) or hasattr(type(engine), "pg_pool")
    ):
        return engine.pg_pool
    return engine


# ---------------------------------------------------------------------------
# GET /api/business-insights/morning-brief
# ---------------------------------------------------------------------------


async def api_business_insights_morning_brief(request: Any) -> JSONResponse:
    """GET /api/business-insights/morning-brief — 12-minute executive morning brief."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_ns = request.query_params.get("namespace_id") or request.query_params.get("namespace")
    ns, err = _require_namespace_id(
        raw_ns,
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    if pool is not None:
        try:
            await require_business_insights_enabled(pool, ns)
        except BusinessInsightsDisabledError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:
            return admin_error_response("Failed checking business insights opt-in status", exc)

    params: dict[str, Any] = {"namespace_id": ns}
    if "lookback_hours" in request.query_params:
        try:
            params["lookback_hours"] = int(request.query_params["lookback_hours"])
        except ValueError as exc:
            return JSONResponse({"error": f"Invalid lookback_hours: {exc}"}, status_code=422)

    if "as_of" in request.query_params:
        params["as_of"] = request.query_params["as_of"]

    try:
        res = await do_morning_brief(admin_state.engine, params)
        return JSONResponse(_json_safe(res))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to generate morning brief", exc)


# ---------------------------------------------------------------------------
# GET /api/business-insights/risk-radar
# ---------------------------------------------------------------------------


async def api_business_insights_risk_radar(request: Any) -> JSONResponse:
    """GET /api/business-insights/risk-radar — cross-engine systemic collision detection."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_ns = request.query_params.get("namespace_id") or request.query_params.get("namespace")
    ns, err = _require_namespace_id(
        raw_ns,
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    if pool is not None:
        try:
            await require_business_insights_enabled(pool, ns)
        except BusinessInsightsDisabledError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:
            return admin_error_response("Failed checking business insights opt-in status", exc)

    params: dict[str, Any] = {"namespace_id": ns}
    if "lookback_days" in request.query_params:
        try:
            params["lookback_days"] = int(request.query_params["lookback_days"])
        except ValueError as exc:
            return JSONResponse({"error": f"Invalid lookback_days: {exc}"}, status_code=422)

    if "as_of" in request.query_params:
        params["as_of"] = request.query_params["as_of"]

    try:
        res = await do_risk_radar(admin_state.engine, params)
        return JSONResponse(_json_safe(res))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to execute risk radar", exc)


# ---------------------------------------------------------------------------
# POST /api/business-insights/run-scenario
# ---------------------------------------------------------------------------


async def api_business_insights_run_scenario(request: Any) -> JSONResponse:
    """POST /api/business-insights/run-scenario — Monte-Carlo what-if scenario simulation."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"error": f"Invalid JSON body: {exc}"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

    raw_ns = body.get("namespace_id") or body.get("namespace")
    ns, err = _require_namespace_id(
        raw_ns,
        missing_error="namespace_id is required in JSON body",
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    if pool is not None:
        try:
            await require_business_insights_enabled(pool, ns)
        except BusinessInsightsDisabledError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:
            return admin_error_response("Failed checking business insights opt-in status", exc)

    name = body.get("name") or body.get("scenario_name")
    if not name or not isinstance(name, str):
        return JSONResponse({"error": "Scenario name is required"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": ns,
        "name": name,
    }
    if "simulation_runs" in body:
        try:
            params["simulation_runs"] = int(body["simulation_runs"])
        except ValueError as exc:
            return JSONResponse({"error": f"Invalid simulation_runs: {exc}"}, status_code=422)

    if "assumptions" in body:
        if not isinstance(body["assumptions"], dict):
            return JSONResponse({"error": "assumptions must be a dict"}, status_code=422)
        params["assumptions"] = body["assumptions"]

    if "horizon_days" in body:
        try:
            params["horizon_days"] = int(body["horizon_days"])
        except ValueError as exc:
            return JSONResponse({"error": f"Invalid horizon_days: {exc}"}, status_code=422)

    try:
        res = await do_run_scenario(admin_state.engine, params)
        return JSONResponse(_json_safe(res))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to run scenario", exc)


# ---------------------------------------------------------------------------
# GET/POST /api/business-insights/board-pack
# ---------------------------------------------------------------------------


async def api_business_insights_board_pack(request: Any) -> JSONResponse:
    """GET/POST /api/business-insights/board-pack — draft board pack generation."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    if request.method == "POST":
        try:
            data = await request.json()
        except Exception as exc:
            return JSONResponse({"error": f"Invalid JSON body: {exc}"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)
    else:
        data = dict(request.query_params)

    raw_ns = data.get("namespace_id") or data.get("namespace")
    ns, err = _require_namespace_id(
        raw_ns,
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    if pool is not None:
        try:
            await require_business_insights_enabled(pool, ns)
        except BusinessInsightsDisabledError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:
            return admin_error_response("Failed checking business insights opt-in status", exc)

    quarter = data.get("quarter")
    if not quarter or not isinstance(quarter, str):
        return JSONResponse({"error": "quarter is required (e.g. 'Q3-2026')"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": ns,
        "quarter": quarter,
    }
    if "meeting_date" in data:
        params["meeting_date"] = data["meeting_date"]
    if "as_of" in data:
        params["as_of"] = data["as_of"]

    try:
        res = await do_generate_board_pack(admin_state.engine, params)
        return JSONResponse(_json_safe(res))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to generate board pack", exc)


# ---------------------------------------------------------------------------
# GET /api/business-insights/kpi-dashboard
# ---------------------------------------------------------------------------


async def api_business_insights_kpi_dashboard(request: Any) -> JSONResponse:
    """GET /api/business-insights/kpi-dashboard — live KPI cockpit and trend analysis."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_ns = request.query_params.get("namespace_id") or request.query_params.get("namespace")
    ns, err = _require_namespace_id(
        raw_ns,
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    if pool is not None:
        try:
            await require_business_insights_enabled(pool, ns)
        except BusinessInsightsDisabledError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:
            return admin_error_response("Failed checking business insights opt-in status", exc)

    params: dict[str, Any] = {"namespace_id": ns}
    if "period" in request.query_params:
        params["period"] = request.query_params["period"]
    if "as_of" in request.query_params:
        params["as_of"] = request.query_params["as_of"]

    try:
        res = await do_kpi_dashboard(admin_state.engine, params)
        return JSONResponse(_json_safe(res))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to retrieve KPI dashboard", exc)


# ---------------------------------------------------------------------------
# POST /api/business-insights/ask
# ---------------------------------------------------------------------------


async def api_business_insights_ask(request: Any) -> JSONResponse:
    """POST /api/business-insights/ask — role-scoped NL query with BI-1 and BI-3 gating."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"error": f"Invalid JSON body: {exc}"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

    raw_ns = body.get("namespace_id") or body.get("namespace")
    ns, err = _require_namespace_id(
        raw_ns,
        missing_error="namespace_id is required in JSON body",
    )
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    if pool is not None:
        try:
            await require_business_insights_enabled(pool, ns)
        except BusinessInsightsDisabledError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:
            return admin_error_response("Failed checking business insights opt-in status", exc)

    query = body.get("query")
    if not query or not isinstance(query, str):
        return JSONResponse({"error": "query string is required"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": ns,
        "query": query,
        "caller_role": body.get("caller_role", "executive"),
        "allow_external_ai": body.get("allow_external_ai", False),
        "board_signoff_reference": body.get("board_signoff_reference"),
    }

    try:
        res = await do_ask_business(admin_state.engine, params)
        return JSONResponse(_json_safe(res))
    except PersonRankingProhibitedError as exc:
        return JSONResponse(
            {"error": str(exc), "code": "person_ranking_prohibited"}, status_code=400
        )
    except ThirdPartyEgressUnauthorizedError as exc:
        return JSONResponse(
            {"error": str(exc), "code": "third_party_egress_unauthorized"}, status_code=403
        )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to answer business query", exc)
