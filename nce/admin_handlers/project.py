"""
Admin HTTP handlers for the Project vertical module (M7.W5: phase-routes).

Exports:
  ``api_project_convert_signed_quote`` — POST /api/project/convert-signed-quote
  ``api_project_get_phase``            — GET  /api/project/{id}/phase
  ``api_project_advance_phase``        — POST /api/project/{id}/phase

All handlers are thin REST wrappers; they contain no business logic, no LLM
in the path.  Logic lives entirely in the ``do_*`` domain cores.

Gate-fail contract:
  ``do_advance_phase`` returns ``{"ok": False, "missing_criteria": [...], ...}``
  when criteria are unmet.  This route translates that to HTTP 409.
  A bad-params / absent-project ``{"ok": False, "error": ...}`` returns 400/404.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
)
from nce.auth import validate_agent_id
from nce.vertical_modules.project.advance import do_advance_phase, read_current_phase
from nce.vertical_modules.project.convert import do_convert_signed_quote

log = logging.getLogger("nce.admin_handlers.project")


# ---------------------------------------------------------------------------
# POST /api/project/convert-signed-quote
# ---------------------------------------------------------------------------


async def api_project_convert_signed_quote(request) -> JSONResponse:
    """POST /api/project/convert-signed-quote

    Convert a signed quote to a Project (Sales→Project bridge).

    Request body (JSON):
        namespace_id  (str, required): Active namespace UUID.
        quote_id      (str, required): Sales QUOTE identifier.
        signed_by     (str, required): Actor who signed.
        signature_ref (str, required): Signature reference.

    Response (JSON):
        {"project_id": str, "gate": str, "bom_lines_linked": int,
         "degraded": bool, "degraded_reasons": [str], "degraded_detail": str|null,
         "baseline": {...}}

    HTTP 200 does NOT mean the project is fully populated.  ``degraded`` is
    True when the conversion succeeded structurally but is incomplete — most
    commonly ``no_bom_lines_in_graph`` (no BOM_LINE nodes exist in NCE for the
    quote, so the project has an empty bill of materials).  The status stays
    200 because the project really was created; clients must inspect
    ``degraded`` rather than infer success from the status code.
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
        validate_agent_id(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    try:
        result = await do_convert_signed_quote(admin_state.engine, body)
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Project convert-signed-quote error",
            exc,
            status_code=500,
            log_event="api_project_convert_signed_quote",
        )


# ---------------------------------------------------------------------------
# GET /api/project/{id}/phase
# ---------------------------------------------------------------------------


async def api_project_get_phase(request) -> JSONResponse:
    """GET /api/project/{id}/phase

    Return the project's current phase gate.

    Path parameters:
        id (str): Project label, e.g. ``PROJECT:Q123``.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"project_id": str, "phase": str | null}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    project_id = request.path_params.get("id", "").strip()
    if not project_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    try:
        phase = await read_current_phase(admin_state.engine, namespace_id, project_id)
        return JSONResponse({"project_id": project_id, "phase": phase})
    except Exception as exc:
        return admin_error_response(
            "Project get-phase error",
            exc,
            status_code=500,
            log_event="api_project_get_phase",
        )


# ---------------------------------------------------------------------------
# POST /api/project/{id}/phase
# ---------------------------------------------------------------------------


async def api_project_advance_phase(request) -> JSONResponse:
    """POST /api/project/{id}/phase

    Advance the project to a new phase gate.

    Path parameters:
        id (str): Project label, e.g. ``PROJECT:Q123``.

    Request body (JSON):
        namespace_id  (str, required): Active namespace UUID.
        target_phase  (str, required): Target gate, e.g. ``"G1"``.
        actor         (str, required): Who is requesting the advance.
        criteria_met  (list[str], optional): Criterion keys currently satisfied.

    Response (JSON) — success:
        {"ok": True, "phase": str}  HTTP 200
    Response (JSON) — gate refused (criteria unmet):
        {"missing_criteria": [...], "current_phase": str}  HTTP 409
    Response (JSON) — bad params / project absent:
        {"error": str}  HTTP 400
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    project_id = request.path_params.get("id", "").strip()
    if not project_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id = str(body.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    try:
        validate_agent_id(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    params = {
        "namespace_id": namespace_id,
        "project_id": project_id,
        "target_phase": body.get("target_phase", ""),
        "actor": body.get("actor", ""),
        "criteria_met": body.get("criteria_met", []),
    }

    try:
        result = await do_advance_phase(admin_state.engine, params)
    except Exception as exc:
        return admin_error_response(
            "Project advance-phase error",
            exc,
            status_code=500,
            log_event="api_project_advance_phase",
        )

    if result.get("ok"):
        return JSONResponse(result)

    # Gate refused with missing_criteria → 409
    if "missing_criteria" in result:
        return JSONResponse(
            {
                "missing_criteria": result["missing_criteria"],
                "current_phase": result.get("current_phase"),
            },
            status_code=409,
        )

    # Bad params / absent project → 400
    return JSONResponse({"error": result.get("error", "advance_phase failed")}, status_code=400)


# ---------------------------------------------------------------------------
# GET /api/project/my-day
# ---------------------------------------------------------------------------


async def api_admin_project_my_day(request) -> JSONResponse:
    """GET /api/project/my-day

    Retrieve and rank open tasks by priority.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        employee_id  (str, optional): Employee identifier.
        reference_date (str, optional): Reference date for sorting.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    params = {
        "namespace_id": namespace_id,
        "employee_id": request.query_params.get("employee_id"),
        "reference_date": request.query_params.get("reference_date"),
    }

    try:
        from nce.vertical_modules.project.pl import do_my_day

        result = await do_my_day(admin_state.engine, params)
        if not result.get("ok"):
            return JSONResponse({"error": result.get("error", "do_my_day failed")}, status_code=400)
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Project my-day error",
            exc,
            status_code=500,
            log_event="api_admin_project_my_day",
        )


# ---------------------------------------------------------------------------
# GET /api/project/capacity
# ---------------------------------------------------------------------------


async def api_admin_project_capacity(request) -> JSONResponse:
    """GET /api/project/capacity

    Aggregate open task load per PL/team over a given window.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        start_date   (str, optional): Start of window.
        end_date     (str, optional): End of window.
        window       (str, optional): Optional helper (e.g. days) or unused.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    params = {
        "namespace_id": namespace_id,
        "start_date": request.query_params.get("start_date"),
        "end_date": request.query_params.get("end_date"),
        "window": request.query_params.get("window"),
    }

    try:
        from nce.vertical_modules.project.pl import do_capacity

        result = await do_capacity(admin_state.engine, params)
        if not result.get("ok"):
            return JSONResponse(
                {"error": result.get("error", "do_capacity failed")}, status_code=400
            )
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Project capacity error",
            exc,
            status_code=500,
            log_event="api_admin_project_capacity",
        )


# ---------------------------------------------------------------------------
# GET /api/project/{id}/scope-creep
# ---------------------------------------------------------------------------


async def api_admin_project_scope_creep(request) -> JSONResponse:
    """GET /api/project/{id}/scope-creep

    Diff current BOM against Sales-frozen baseline.

    Path parameters:
        id (str): Project label, e.g. ``PROJECT:Q123``.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    project_id = request.path_params.get("id", "").strip()
    if not project_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    params = {
        "namespace_id": namespace_id,
        "project_id": project_id,
    }

    try:
        from nce.vertical_modules.project.insights import do_detect_scope_creep

        result = await do_detect_scope_creep(admin_state.engine, params)
        if not result.get("ok"):
            return JSONResponse(
                {"error": result.get("error", "do_detect_scope_creep failed")}, status_code=400
            )
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Project scope-creep error",
            exc,
            status_code=500,
            log_event="api_admin_project_scope_creep",
        )


# ---------------------------------------------------------------------------
# GET /api/project/{id}/status-report
# ---------------------------------------------------------------------------


async def api_admin_project_status_report(request) -> JSONResponse:
    """GET /api/project/{id}/status-report

    Generate status report narrative and margin-trinity snapshot.

    Path parameters:
        id (str): Project label, e.g. ``PROJECT:Q123``.

    Query parameters:
        namespace_id          (str, required): Active namespace UUID.
        estimated_cost_nok    (float, optional): Custom estimated cost.
        estimated_revenue_nok (float, optional): Custom estimated revenue.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    project_id = request.path_params.get("id", "").strip()
    if not project_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required query param: namespace_id"}, status_code=422
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

    est_cost = request.query_params.get("estimated_cost_nok")
    est_rev = request.query_params.get("estimated_revenue_nok")

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "project_id": project_id,
    }
    if est_cost is not None:
        try:
            params["estimated_cost_nok"] = float(est_cost)
        except ValueError:
            return JSONResponse(
                {"error": "Invalid estimated_cost_nok query param"}, status_code=422
            )
    if est_rev is not None:
        try:
            params["estimated_revenue_nok"] = float(est_rev)
        except ValueError:
            return JSONResponse(
                {"error": "Invalid estimated_revenue_nok query param"}, status_code=422
            )

    try:
        from nce.vertical_modules.project.insights import do_status_report

        result = await do_status_report(admin_state.engine, params)
        if not result.get("ok"):
            return JSONResponse(
                {"error": result.get("error", "do_status_report failed")}, status_code=400
            )
        return JSONResponse(result)
    except Exception as exc:
        return admin_error_response(
            "Project status-report error",
            exc,
            status_code=500,
            log_event="api_admin_project_status_report",
        )
