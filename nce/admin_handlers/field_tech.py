"""
nce/admin_handlers/field_tech.py
================================
Admin HTTP handlers for Module 12 (Field Tech Engine):
  - api_field_tech_dispatch: POST /api/field-tech/dispatch
  - api_field_tech_create_work_order: POST /api/field-tech/work-orders
  - api_field_tech_query_work_orders: GET /api/field-tech/work-orders
  - api_field_tech_work_order: GET /api/field-tech/work-orders/{id}
  - api_field_tech_assign: POST /api/field-tech/work-orders/{id}/assign
  - api_field_tech_complete_checklist: POST /api/field-tech/checklists
  - api_field_tech_scan_serial: POST /api/field-tech/scans
  - api_field_tech_log_time: POST /api/field-tech/time-entries
  - api_field_tech_attach_photo: POST /api/field-tech/photos
  - api_field_tech_sync: POST /api/field-tech/sync
  - api_field_tech_record_outcome: POST /api/field-tech/outcomes
  - api_field_tech_partner_view: GET /api/field-tech/partner-view

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
from nce.vertical_modules.field_tech._guard import (
    FieldTechDisabledError,
    require_field_tech_enabled,
)
from nce.vertical_modules.field_tech.checklist import (
    ChecklistIncompleteError,
    ChecklistNotFoundError,
    do_complete_checklist,
)
from nce.vertical_modules.field_tech.dispatch import do_dispatch
from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.field_tech.partner_view import do_partner_view
from nce.vertical_modules.field_tech.photo import do_attach_photo
from nce.vertical_modules.field_tech.scan import do_scan_serial
from nce.vertical_modules.field_tech.sync import do_sync
from nce.vertical_modules.field_tech.time_entry import do_log_time
from nce.vertical_modules.field_tech.work_orders import (
    WorkOrderInvalidTransitionError,
    WorkOrderNotFoundError,
    do_assign,
    do_create_work_order,
    do_get_work_order,
    do_query_work_order,
)

log = logging.getLogger("nce.admin_handlers.field_tech")


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
# POST /api/field-tech/dispatch
# ---------------------------------------------------------------------------


async def api_field_tech_dispatch(request: Any) -> JSONResponse:
    """POST /api/field-tech/dispatch — rank candidate technicians for a work order."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_dispatch(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_dispatch error: %s", exc)
        return admin_error_response("Internal dispatch error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/work-orders
# ---------------------------------------------------------------------------


async def api_field_tech_create_work_order(request: Any) -> JSONResponse:
    """POST /api/field-tech/work-orders — create a new work order."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_create_work_order(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "work_order": res}, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_create_work_order error: %s", exc)
        return admin_error_response("Internal create work order error", exc)


# ---------------------------------------------------------------------------
# GET /api/field-tech/work-orders
# ---------------------------------------------------------------------------


async def api_field_tech_query_work_orders(request: Any) -> JSONResponse:
    """GET /api/field-tech/work-orders — list work orders with filters."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "status": request.query_params.get("status"),
        "kind": request.query_params.get("kind"),
        "assignee_id": request.query_params.get("assignee_id"),
        "location_id": request.query_params.get("location_id"),
        "partner_scope_id": request.query_params.get("partner_scope_id"),
    }

    try:
        res = await do_query_work_order(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_query_work_orders error: %s", exc)
        return admin_error_response("Internal query work orders error", exc)


# ---------------------------------------------------------------------------
# GET /api/field-tech/work-orders/{id}
# ---------------------------------------------------------------------------


async def api_field_tech_work_order(request: Any) -> JSONResponse:
    """GET /api/field-tech/work-orders/{id} — get work order details."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    wo_id = request.path_params.get("id")
    if not wo_id:
        return JSONResponse({"error": "work_order_id path parameter is required"}, status_code=422)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    try:
        res = await do_get_work_order(
            admin_state.engine, {"namespace_id": namespace_id, "work_order_id": wo_id}
        )
        return JSONResponse({"ok": True, "work_order": res})
    except WorkOrderNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_work_order error: %s", exc)
        return admin_error_response("Internal get work order error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/work-orders/{id}/assign
# ---------------------------------------------------------------------------


async def api_field_tech_assign(request: Any) -> JSONResponse:
    """POST /api/field-tech/work-orders/{id}/assign — assign work order."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    wo_id = request.path_params.get("id")
    if not wo_id:
        return JSONResponse({"error": "work_order_id path parameter is required"}, status_code=422)

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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id
    params["work_order_id"] = wo_id

    try:
        res = await do_assign(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "assignment": res})
    except WorkOrderNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except WorkOrderInvalidTransitionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_assign error: %s", exc)
        return admin_error_response("Internal assign work order error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/checklists
# ---------------------------------------------------------------------------


async def api_field_tech_complete_checklist(request: Any) -> JSONResponse:
    """POST /api/field-tech/checklists — complete checklist verification."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_complete_checklist(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "checklist": res})
    except ChecklistIncompleteError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ChecklistNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_complete_checklist error: %s", exc)
        return admin_error_response("Internal complete checklist error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/scans
# ---------------------------------------------------------------------------


async def api_field_tech_scan_serial(request: Any) -> JSONResponse:
    """POST /api/field-tech/scans — record equipment serial scan."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_scan_serial(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "scan": res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_scan_serial error: %s", exc)
        return admin_error_response("Internal scan serial error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/time-entries
# ---------------------------------------------------------------------------


async def api_field_tech_log_time(request: Any) -> JSONResponse:
    """POST /api/field-tech/time-entries — log labor time entry."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_log_time(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "time_entry": res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_log_time error: %s", exc)
        return admin_error_response("Internal log time error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/photos
# ---------------------------------------------------------------------------


async def api_field_tech_attach_photo(request: Any) -> JSONResponse:
    """POST /api/field-tech/photos — attach documentation photo."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_attach_photo(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "photo": res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_attach_photo error: %s", exc)
        return admin_error_response("Internal attach photo error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/sync
# ---------------------------------------------------------------------------


async def api_field_tech_sync(request: Any) -> JSONResponse:
    """POST /api/field-tech/sync — reconcile offline client mutation queue."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_sync(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "sync": res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_sync error: %s", exc)
        return admin_error_response("Internal sync error", exc)


# ---------------------------------------------------------------------------
# POST /api/field-tech/outcomes
# ---------------------------------------------------------------------------


async def api_field_tech_record_outcome(request: Any) -> JSONResponse:
    """POST /api/field-tech/outcomes — record completion outcome in v3_cognitive_ledger."""
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
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        res = await do_record_outcome(admin_state.engine, params)
        bump_mcp_cache_generation(admin_state.engine)
        return JSONResponse({"ok": True, "outcome": res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_record_outcome error: %s", exc)
        return admin_error_response("Internal record outcome error", exc)


# ---------------------------------------------------------------------------
# GET /api/field-tech/partner-view
# ---------------------------------------------------------------------------


async def api_field_tech_partner_view(request: Any) -> JSONResponse:
    """GET /api/field-tech/partner-view — partner-scoped & redacted projection."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err:
        return err

    partner_scope_id = request.query_params.get("partner_scope_id")
    if not partner_scope_id:
        return JSONResponse(
            {"error": "partner_scope_id query parameter is required"}, status_code=422
        )

    pool = _extract_pool(admin_state.engine)
    try:
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "partner_scope_id": partner_scope_id,
        "work_order_id": request.query_params.get("work_order_id"),
    }

    try:
        res = await do_partner_view(admin_state.engine, params)
        return JSONResponse({"ok": True, **res})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_field_tech_partner_view error: %s", exc)
        return admin_error_response("Internal partner view error", exc)
