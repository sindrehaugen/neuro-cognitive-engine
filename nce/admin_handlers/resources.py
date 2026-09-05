"""
nce/admin_handlers/resources.py
===============================
Admin HTTP REST Handlers for Module 15: Staff & Resources Engine (ML15-B7).

Endpoints:
  - GET  /api/resources/capacity       — api_resources_resolve_capacity
  - POST /api/resources/plan-allocation — api_resources_plan_allocation
  - POST /api/resources/reserve        — api_resources_reserve (mutating)
  - POST /api/resources/release        — api_resources_release (mutating)
  - GET  /api/resources/conflicts      — api_resources_detect_conflicts
  - POST /api/resources/material-flow  — api_resources_plan_material_flow (mutating)
  - POST /api/resources/travel         — api_resources_plan_travel (mutating)
  - GET  /api/resources/field-schedule — api_resources_field_schedule
  - GET  /api/resources/forecast       — api_resources_forecast_demand
  - GET  /api/resources/pulse          — api_resources_capacity_pulse

All mutating routes bump MCP cache generation via bump_mcp_cache_generation.
All queries enforce explicit tenant predicates: WHERE namespace_id = $1.
All exception handlers strictly pass (message, exc) to admin_error_response.
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
    bump_mcp_cache_generation,
)
from nce.vertical_modules.resources._guard import (
    ResourceConcurrencyError,
    ResourceNotFoundError,
    ResourcesDisabledError,
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import (
    do_detect_conflicts,
    do_release,
    do_reserve,
)
from nce.vertical_modules.resources.capacity import do_resolve_capacity
from nce.vertical_modules.resources.field_schedule import do_field_schedule
from nce.vertical_modules.resources.forecast import (
    do_forecast_demand,
    get_morning_brief_capacity_pulse,
)
from nce.vertical_modules.resources.material_flow import do_plan_material_flow
from nce.vertical_modules.resources.planner import do_plan_allocation
from nce.vertical_modules.resources.travel import do_plan_travel

log = logging.getLogger("nce.admin_handlers.resources")


def _check_enabled(params: dict[str, Any]) -> None:
    require_resources_enabled(params.get("namespace_metadata"))


# ---------------------------------------------------------------------------
# GET /api/resources/capacity
# ---------------------------------------------------------------------------


async def api_resources_resolve_capacity(request: Any) -> JSONResponse:
    """GET /api/resources/capacity — resolve capacity calendar for resources."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    params: dict[str, Any] = {"namespace_id": ns}
    for key in ("starts_at", "ends_at", "resource_id", "kind"):
        if key in request.query_params:
            params[key] = request.query_params[key]

    try:
        _check_enabled(params)
        result = await do_resolve_capacity(admin_state.engine, params)
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to resolve capacity", exc)


# ---------------------------------------------------------------------------
# POST /api/resources/plan-allocation
# ---------------------------------------------------------------------------


async def api_resources_plan_allocation(request: Any) -> JSONResponse:
    """POST /api/resources/plan-allocation — AI allocation advisor."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    try:
        _check_enabled(body)
        result = await do_plan_allocation(admin_state.engine, body)
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to plan allocation", exc)


# ---------------------------------------------------------------------------
# POST /api/resources/reserve
# ---------------------------------------------------------------------------


async def api_resources_reserve(request: Any) -> JSONResponse:
    """POST /api/resources/reserve — book resource allocation (RS-3 exclusion)."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    try:
        _check_enabled(body)
        result = await do_reserve(admin_state.engine, body)
        await bump_mcp_cache_generation(admin_state.engine, route="api_resources_reserve")
        return JSONResponse(_json_safe(result), status_code=201)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ResourceConcurrencyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ResourceNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to reserve resource", exc)


# ---------------------------------------------------------------------------
# POST /api/resources/release
# ---------------------------------------------------------------------------


async def api_resources_release(request: Any) -> JSONResponse:
    """POST /api/resources/release — release active allocation."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    try:
        _check_enabled(body)
        result = await do_release(admin_state.engine, body)
        await bump_mcp_cache_generation(admin_state.engine, route="api_resources_release")
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ResourceNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to release allocation", exc)


# ---------------------------------------------------------------------------
# GET /api/resources/conflicts
# ---------------------------------------------------------------------------


async def api_resources_detect_conflicts(request: Any) -> JSONResponse:
    """GET /api/resources/conflicts — detect schedule conflicts and double-bookings."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    params: dict[str, Any] = {"namespace_id": ns}
    for key in ("resource_id", "starts_at", "ends_at"):
        if key in request.query_params:
            params[key] = request.query_params[key]

    try:
        _check_enabled(params)
        result = await do_detect_conflicts(admin_state.engine, params)
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to detect conflicts", exc)


# ---------------------------------------------------------------------------
# POST /api/resources/material-flow
# ---------------------------------------------------------------------------


async def api_resources_plan_material_flow(request: Any) -> JSONResponse:
    """POST /api/resources/material-flow — plan warehouse staging & van load (RS-2)."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    try:
        _check_enabled(body)
        result = await do_plan_material_flow(admin_state.engine, body)
        await bump_mcp_cache_generation(
            admin_state.engine, route="api_resources_plan_material_flow"
        )
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ResourceNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to plan material flow", exc)


# ---------------------------------------------------------------------------
# POST /api/resources/travel
# ---------------------------------------------------------------------------


async def api_resources_plan_travel(request: Any) -> JSONResponse:
    """POST /api/resources/travel — plan/book travel behind Contract-B spend gate (RS-5)."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=422)

    ns, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    try:
        _check_enabled(body)
        result = await do_plan_travel(admin_state.engine, body)
        if body.get("action") == "book":
            await bump_mcp_cache_generation(admin_state.engine, route="api_resources_plan_travel")
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ResourceNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to plan travel", exc)


# ---------------------------------------------------------------------------
# GET /api/resources/field-schedule
# ---------------------------------------------------------------------------


async def api_resources_field_schedule(request: Any) -> JSONResponse:
    """GET /api/resources/field-schedule — field webapp unified technician read model."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    resource_id = request.query_params.get("resource_id")
    if not resource_id:
        return JSONResponse({"error": "resource_id query parameter is required"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": ns,
        "resource_id": resource_id,
    }
    for key in ("starts_at", "ends_at", "date_from", "date_to"):
        if key in request.query_params:
            params[key] = request.query_params[key]

    try:
        _check_enabled(params)
        result = await do_field_schedule(admin_state.engine, params)
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ResourceNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to retrieve field schedule", exc)


# ---------------------------------------------------------------------------
# GET /api/resources/forecast
# ---------------------------------------------------------------------------


async def api_resources_forecast_demand(request: Any) -> JSONResponse:
    """GET /api/resources/forecast — demand forecast & capacity gap intelligence."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    params: dict[str, Any] = {"namespace_id": ns}
    if "horizon_days" in request.query_params:
        try:
            params["horizon_days"] = int(request.query_params["horizon_days"])
        except ValueError:
            return JSONResponse({"error": "horizon_days must be an integer"}, status_code=422)
    for key in ("role", "kind"):
        if key in request.query_params:
            params[key] = request.query_params[key]

    try:
        _check_enabled(params)
        result = await do_forecast_demand(admin_state.engine, params)
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (ResourceValidationError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to forecast demand", exc)


# ---------------------------------------------------------------------------
# GET /api/resources/pulse
# ---------------------------------------------------------------------------


async def api_resources_capacity_pulse(request: Any) -> JSONResponse:
    """GET /api/resources/pulse — daily capacity pulse for Morning Brief."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ns, err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if err is not None:
        return err

    params: dict[str, Any] = {"namespace_id": ns}
    try:
        _check_enabled(params)
        result = await get_morning_brief_capacity_pulse(admin_state.engine, params)
        return JSONResponse(_json_safe(result), status_code=200)
    except ResourcesDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        return admin_error_response("Failed to retrieve capacity pulse", exc)
