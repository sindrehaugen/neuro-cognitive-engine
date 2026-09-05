"""
nce/admin_handlers/hr.py
========================
Admin HTTP handlers for Module 13 (HR Engine):
  - api_hr_employees: GET /api/hr/employees (list employees, field-scoped)
  - api_hr_create_employee: POST /api/hr/employees (register employee card)
  - api_hr_employee: GET /api/hr/employees/{id} (profile card)
  - api_hr_match_skills: POST /api/hr/match-skills (pure requirement fit)
  - api_hr_capacity: GET /api/hr/capacity (utilization dashboard)
  - api_hr_cert_status: GET /api/hr/cert-status (Watcher cert-expiry board)
  - api_hr_register_absence: POST /api/hr/absences (Smart Leave intake)
  - api_hr_onboarding_get: GET /api/hr/onboarding/{id} (quest checklist)
  - api_hr_onboarding_build: POST /api/hr/onboarding/{id} (advance/build quest)
  - api_hr_coach: POST /api/hr/coach (private skill advisor, NEVER ranking)
  - api_hr_sync_status: GET /api/hr/sync/status (backend status)
  - api_hr_sync_now: POST /api/hr/sync/now (trigger sync)

All mutating routes call ``bump_mcp_cache_generation(admin_state.engine)``
to invalidate the MCP response cache upon state mutation.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.admin_handlers._shared import (
    JSONResponse,
    _require_namespace_id,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
)
from nce.vertical_modules.hr._guard import (
    HrDisabledError,
    HrRankingProhibitedError,
    require_hr_enabled,
)
from nce.vertical_modules.hr.absences import do_register_absence
from nce.vertical_modules.hr.capacity import do_capacity
from nce.vertical_modules.hr.certs import do_cert_status
from nce.vertical_modules.hr.coaching import do_coach
from nce.vertical_modules.hr.onboarding import do_build_onboarding_quest
from nce.vertical_modules.hr.profile import (
    do_create_employee,
    do_get_employee,
    do_query_employees,
)
from nce.vertical_modules.hr.skills import do_match_skills

log = logging.getLogger("nce.admin_handlers.hr")


def _extract_pool(engine: Any) -> Any:
    if hasattr(engine, "pg_pool") and (
        "pg_pool" in getattr(engine, "__dict__", {}) or hasattr(type(engine), "pg_pool")
    ):
        return engine.pg_pool
    return engine


async def _parse_json_body(request: Any) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return None, JSONResponse(
                {"error": "Request body must be a JSON object"}, status_code=422
            )
        return data, None
    except Exception as exc:
        return None, JSONResponse({"error": f"Invalid JSON body: {exc}"}, status_code=422)


# ---------------------------------------------------------------------------
# GET /api/hr/employees
# ---------------------------------------------------------------------------


async def api_hr_employees(request: Any) -> JSONResponse:
    """GET /api/hr/employees — list and filter employees in namespace."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "caller_role": request.query_params.get("caller_role", "peer"),
    }
    for field in ("department", "role", "location_id", "limit", "offset"):
        val = request.query_params.get(field)
        if val is not None:
            params[field] = val
    if "active" in request.query_params:
        params["active"] = request.query_params["active"].lower() == "true"

    try:
        res = await do_query_employees(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_employees error: %s", exc)
        return admin_error_response("Internal employee query error", exc)


# ---------------------------------------------------------------------------
# POST /api/hr/employees
# ---------------------------------------------------------------------------


async def api_hr_create_employee(request: Any) -> JSONResponse:
    """POST /api/hr/employees — create or register an employee profile card."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err = await _parse_json_body(request)
    if err:
        return err

    namespace_id, err = _require_namespace_id(
        request.query_params.get("namespace_id") or body.get("namespace_id")
    )
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_create_employee(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, **res}, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_create_employee error: %s", exc)
        return admin_error_response("Internal employee create error", exc)


# ---------------------------------------------------------------------------
# GET /api/hr/employees/{id}
# ---------------------------------------------------------------------------


async def api_hr_employee(request: Any) -> JSONResponse:
    """GET /api/hr/employees/{id} — get employee card, skills, and certifications."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    employee_id = request.path_params.get("id")
    if not employee_id:
        return JSONResponse({"error": "Missing employee id path parameter"}, status_code=422)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "employee_id": employee_id,
        "caller_role": request.query_params.get("caller_role", "peer"),
        "caller_id": request.query_params.get("caller_id"),
    }

    try:
        res = await do_get_employee(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_hr_employee error: %s", exc)
        return admin_error_response("Internal employee retrieval error", exc)


# ---------------------------------------------------------------------------
# POST /api/hr/match-skills
# ---------------------------------------------------------------------------


async def api_hr_match_skills(request: Any) -> JSONResponse:
    """POST /api/hr/match-skills — match candidate skill requirements (RL-1: NEVER ranking)."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err = await _parse_json_body(request)
    if err:
        return err

    namespace_id, err = _require_namespace_id(
        request.query_params.get("namespace_id") or body.get("namespace_id")
    )
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_match_skills(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except HrRankingProhibitedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_match_skills error: %s", exc)
        return admin_error_response("Internal skills match error", exc)


# ---------------------------------------------------------------------------
# GET /api/hr/capacity
# ---------------------------------------------------------------------------


async def api_hr_capacity(request: Any) -> JSONResponse:
    """GET /api/hr/capacity — workload and utilization dashboard."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {"namespace_id": namespace_id}
    if "employee_id" in request.query_params:
        params["employee_id"] = request.query_params["employee_id"]
    if "department" in request.query_params:
        params["department"] = request.query_params["department"]
    if "horizon_days" in request.query_params:
        params["horizon_days"] = request.query_params["horizon_days"]

    try:
        res = await do_capacity(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_capacity error: %s", exc)
        return admin_error_response("Internal capacity error", exc)


# ---------------------------------------------------------------------------
# GET /api/hr/cert-status
# ---------------------------------------------------------------------------


async def api_hr_cert_status(request: Any) -> JSONResponse:
    """GET /api/hr/cert-status — certification validity and impending expiration board."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {"namespace_id": namespace_id}
    if "employee_id" in request.query_params:
        params["employee_id"] = request.query_params["employee_id"]
    if "warn_days" in request.query_params:
        params["warn_days"] = request.query_params["warn_days"]
    if "status" in request.query_params:
        params["status"] = request.query_params["status"]

    try:
        res = await do_cert_status(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_cert_status error: %s", exc)
        return admin_error_response("Internal cert status error", exc)


# ---------------------------------------------------------------------------
# POST /api/hr/absences
# ---------------------------------------------------------------------------


async def api_hr_register_absence(request: Any) -> JSONResponse:
    """POST /api/hr/absences — register employee absence or leave event."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err = await _parse_json_body(request)
    if err:
        return err

    namespace_id, err = _require_namespace_id(
        request.query_params.get("namespace_id") or body.get("namespace_id")
    )
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_register_absence(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, **res}, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_register_absence error: %s", exc)
        return admin_error_response("Internal absence error", exc)


# ---------------------------------------------------------------------------
# GET /api/hr/onboarding/{id}
# ---------------------------------------------------------------------------


async def api_hr_onboarding_get(request: Any) -> JSONResponse:
    """GET /api/hr/onboarding/{id} — get 90-day quest checklist for employee."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    employee_id = request.path_params.get("id")
    if not employee_id:
        return JSONResponse({"error": "Missing employee id path parameter"}, status_code=422)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "employee_id": employee_id,
    }
    if "role" in request.query_params:
        params["role"] = request.query_params["role"]
    if "department" in request.query_params:
        params["department"] = request.query_params["department"]

    try:
        res = await do_build_onboarding_quest(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_onboarding_get error: %s", exc)
        return admin_error_response("Internal onboarding error", exc)


# ---------------------------------------------------------------------------
# POST /api/hr/onboarding/{id}
# ---------------------------------------------------------------------------


async def api_hr_onboarding_build(request: Any) -> JSONResponse:
    """POST /api/hr/onboarding/{id} — generate or advance onboarding quest."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    employee_id = request.path_params.get("id")
    if not employee_id:
        return JSONResponse({"error": "Missing employee id path parameter"}, status_code=422)

    body, err = await _parse_json_body(request)
    if err:
        return err

    namespace_id, err = _require_namespace_id(
        request.query_params.get("namespace_id") or body.get("namespace_id")
    )
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id
    params["employee_id"] = employee_id

    try:
        res = await do_build_onboarding_quest(admin_state.engine, params)
        await bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, **res}, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_onboarding_build error: %s", exc)
        return admin_error_response("Internal onboarding error", exc)


# ---------------------------------------------------------------------------
# POST /api/hr/coach
# ---------------------------------------------------------------------------


async def api_hr_coach(request: Any) -> JSONResponse:
    """POST /api/hr/coach — individual skill advisor (RL-1 NEVER ranking)."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err = await _parse_json_body(request)
    if err:
        return err

    namespace_id, err = _require_namespace_id(
        request.query_params.get("namespace_id") or body.get("namespace_id")
    )
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_coach(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except HrRankingProhibitedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_hr_coach error: %s", exc)
        return admin_error_response("Internal coach error", exc)


# ---------------------------------------------------------------------------
# GET /api/hr/sync/status
# ---------------------------------------------------------------------------


async def api_hr_sync_status(request: Any) -> JSONResponse:
    """GET /api/hr/sync/status — check backend sync health."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse({"ok": True, "status": "idle", "last_sync": None, "synced_records": 0})


# ---------------------------------------------------------------------------
# POST /api/hr/sync/now
# ---------------------------------------------------------------------------


async def api_hr_sync_now(request: Any) -> JSONResponse:
    """POST /api/hr/sync/now — trigger manual sync."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err = await _parse_json_body(request)
    if err:
        return err

    namespace_id, err = _require_namespace_id(
        request.query_params.get("namespace_id") or body.get("namespace_id")
    )
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    await bump_mcp_cache_generation(admin_state.engine)
    return JSONResponse({"ok": True, "status": "completed", "synced_records": 0})
