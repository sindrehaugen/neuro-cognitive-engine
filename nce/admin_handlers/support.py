"""
nce/admin_handlers/support.py
=============================
Admin HTTP handlers for the Support vertical module (Module 10, Wave 6 —
ML10-B6, ``support-rest-surface``).

Exports:
  ``api_support_tickets_list``     — GET  /api/support/tickets
  ``api_support_tickets_open``     — POST /api/support/tickets
  ``api_support_tickets_get``      — GET  /api/support/tickets/{id}
  ``api_support_ticket_sla_clock`` — GET  /api/support/tickets/{id}/sla-clock
  ``api_support_customer_health``  — GET  /api/support/customers/{id}/health
  ``api_support_troubleshoot``     — POST /api/support/troubleshoot
  ``api_support_tickets_resolve``  — POST /api/support/tickets/{id}/resolve

All handlers are thin REST wrappers over the vertical module cores in
``nce/vertical_modules/support/**`` (``do_open_ticket``, ``do_query_ticket``,
``do_sla_clock``, ``do_health_score``, ``do_troubleshoot``, ``do_resolve_ticket``)
— adhering to the "one core function, two surfaces" pattern.

Mutating routes invalidate the MCP response cache via ``bump_mcp_cache_generation``.

Error mapping:
  missing/invalid ``namespace_id`` or path ``id``    -> 422
  ``ValueError`` from a core                         -> 422
  support vertical not enabled                       -> 409
  ticket absent (GET/resolve/troubleshoot)           -> 404
  ticket already resolved / invalid status           -> 409
  anything else                                      -> 500 via ``admin_error_response``
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
from nce.vertical_modules.support._guard import (
    SupportDisabledError,
    require_support_enabled,
)
from nce.vertical_modules.support.health import do_health_score
from nce.vertical_modules.support.sla import do_sla_clock
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketAlreadyResolvedError,
    TicketNotFoundError,
    do_open_ticket,
    do_query_ticket,
    do_resolve_ticket,
)
from nce.vertical_modules.support.troubleshoot import do_troubleshoot

log = logging.getLogger("nce.admin_handlers.support")


def _extract_pool(engine: Any) -> Any:
    """Extract an asyncpg pool from the engine."""
    if hasattr(engine, "pg_pool") and (
        "pg_pool" in getattr(engine, "__dict__", {}) or hasattr(type(engine), "pg_pool")
    ):
        return engine.pg_pool
    return engine


# ---------------------------------------------------------------------------
# GET /api/support/tickets
# ---------------------------------------------------------------------------


async def api_support_tickets_list(request: Any) -> JSONResponse:
    """GET /api/support/tickets — list support tickets with filters.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        status (str, optional): filter by ticket status.
        priority (str, optional): filter by ticket priority.
        customer_id (str, optional): filter by customer ID.
        room_id (str, optional): filter by room / functional location ID.
        asset_id (str, optional): filter by asset UUID.
        limit (int, optional): maximum items to return (default 50).
        offset (int, optional): pagination offset (default 0).

    Response (JSON):
        {"ok": True, "items": [...], "total": int, "limit": int, "offset": int}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    params: dict[str, Any] = {"namespace_id": namespace_id}
    for key in ("status", "priority", "customer_id", "room_id", "asset_id"):
        val = request.query_params.get(key)
        if val is not None and val.strip():
            params[key] = val.strip()

    if "limit" in request.query_params:
        try:
            params["limit"] = int(request.query_params["limit"])
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=422)

    if "offset" in request.query_params:
        try:
            params["offset"] = int(request.query_params["offset"])
        except ValueError:
            return JSONResponse({"error": "offset must be an integer"}, status_code=422)

    try:
        result = await do_query_ticket(admin_state.engine, params)
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support tickets list error",
            exc,
            status_code=500,
            log_event="api_support_tickets_list",
        )


# ---------------------------------------------------------------------------
# POST /api/support/tickets
# ---------------------------------------------------------------------------


async def api_support_tickets_open(request: Any) -> JSONResponse:
    """POST /api/support/tickets — open a new service ticket.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        summary (str, required): short summary description.
        description (str, optional): detailed description.
        priority (str, optional): low/medium/high/critical.
        source (str, optional): nce/d365.
        source_id (str, optional): external source ID.
        customer_id (str, optional): customer ID.
        room_id (str, optional): functional location / room ID.
        asset_id (str, optional): asset UUID.
        sla_profile (str, optional): SLA profile name.
        change_origin (str, optional): origin slug.
        ai_diagnosis (dict, optional): preliminary AI diagnosis.

    Response (JSON):
        {"ok": True, "ticket": {...}, "sla_clock": {...}}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        result = await do_open_ticket(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support ticket open error",
            exc,
            status_code=500,
            log_event="api_support_tickets_open",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_support_tickets_open")
    return JSONResponse({"ok": True, **result}, status_code=201)


# ---------------------------------------------------------------------------
# GET /api/support/tickets/{id}
# ---------------------------------------------------------------------------


async def api_support_tickets_get(request: Any) -> JSONResponse:
    """GET /api/support/tickets/{id} — get a single ticket and its SLA clock.

    Path parameter:
        id (str): Ticket UUID.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"ok": True, "ticket": {...}, "sla_clock": {...}}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ticket_id = request.path_params.get("id", "").strip()
    if not ticket_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    try:
        result = await do_query_ticket(
            admin_state.engine,
            {"namespace_id": namespace_id, "ticket_id": ticket_id},
        )
        return JSONResponse({"ok": True, **result})
    except TicketNotFoundError as exc:
        return JSONResponse(
            {"error": str(exc), "not_found": True, "ticket_id": exc.ticket_id},
            status_code=404,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support ticket get error",
            exc,
            status_code=500,
            log_event="api_support_tickets_get",
        )


# ---------------------------------------------------------------------------
# GET /api/support/tickets/{id}/sla-clock
# ---------------------------------------------------------------------------


async def api_support_ticket_sla_clock(request: Any) -> JSONResponse:
    """GET /api/support/tickets/{id}/sla-clock — get SLA clock countdowns & status.

    Path parameter:
        id (str): Ticket UUID.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"ok": True, "ticket_id": ..., "sla_profile": ..., "breached": bool, ...}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ticket_id = request.path_params.get("id", "").strip()
    if not ticket_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    try:
        result = await do_sla_clock(
            admin_state.engine,
            {"namespace_id": namespace_id, "ticket_id": ticket_id},
        )
        return JSONResponse({"ok": True, **result})
    except TicketNotFoundError as exc:
        return JSONResponse(
            {"error": str(exc), "not_found": True, "ticket_id": exc.ticket_id},
            status_code=404,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support ticket SLA clock error",
            exc,
            status_code=500,
            log_event="api_support_ticket_sla_clock",
        )


# ---------------------------------------------------------------------------
# GET /api/support/customers/{id}/health
# ---------------------------------------------------------------------------


async def api_support_customer_health(request: Any) -> JSONResponse:
    """GET /api/support/customers/{id}/health — get customer health score & churn risk.

    Path parameter:
        id (str): Customer ID.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        lookback_days (int, optional): Lookback window in days (default 30).

    Response (JSON):
        {"ok": True, "customer_id": ..., "score": float, "churn_risk": ..., "trend": ...}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    customer_id = request.path_params.get("id", "").strip()
    if not customer_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "customer_id": customer_id,
    }
    if "lookback_days" in request.query_params:
        try:
            params["lookback_days"] = int(request.query_params["lookback_days"])
        except ValueError:
            return JSONResponse({"error": "lookback_days must be an integer"}, status_code=422)

    try:
        result = await do_health_score(admin_state.engine, params)
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support customer health error",
            exc,
            status_code=500,
            log_event="api_support_customer_health",
        )


# ---------------------------------------------------------------------------
# POST /api/support/troubleshoot
# ---------------------------------------------------------------------------


async def api_support_troubleshoot(request: Any) -> JSONResponse:
    """POST /api/support/troubleshoot — AI Troubleshooter cognitive recall.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        symptom_text (str, optional): observed symptoms.
        ticket_id (str, optional): ticket UUID to diagnose.
        asset_id (str, optional): target asset UUID.
        limit (int, optional): maximum citations to return (default 5).
        min_confidence (float, optional): minimum confidence threshold (default 0.5).

    Response (JSON):
        {"ok": True, "cited_ticket_ids": [...], "proposed_resolution": ..., "citations": [...], "zero_history": bool}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        result = await do_troubleshoot(admin_state.engine, params)
        return JSONResponse({"ok": True, **result})
    except TicketNotFoundError as exc:
        return JSONResponse(
            {"error": str(exc), "not_found": True, "ticket_id": exc.ticket_id},
            status_code=404,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support troubleshoot error",
            exc,
            status_code=500,
            log_event="api_support_troubleshoot",
        )


# ---------------------------------------------------------------------------
# POST /api/support/tickets/{id}/resolve
# ---------------------------------------------------------------------------


async def api_support_tickets_resolve(request: Any) -> JSONResponse:
    """POST /api/support/tickets/{id}/resolve — resolve ticket and update ledger.

    Path parameter:
        id (str): Ticket UUID.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        resolution_text (str, required): detailed resolution description.
        was_fix (bool, optional): whether action fixed underlying cause (default True).
        resolution_category (str, optional): category slug (default 'other').
        fixed_asset_id (str, optional): asset UUID.
        fixed_product_id (str, optional): product ID.
        resolved_by (str, optional): resolving agent / operator ID.

    Response (JSON):
        {"ok": True, "ticket": {...}, "sla_clock": {...}, "resolution_id": str}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ticket_id = request.path_params.get("id", "").strip()
    if not ticket_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id
    params["ticket_id"] = ticket_id

    try:
        result = await do_resolve_ticket(admin_state.engine, params)
    except TicketNotFoundError as exc:
        return JSONResponse(
            {"error": str(exc), "not_found": True, "ticket_id": exc.ticket_id},
            status_code=404,
        )
    except TicketAlreadyResolvedError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "reason": "ticket_already_resolved",
                "ticket_id": exc.ticket_id,
                "status": exc.status,
            },
            status_code=409,
        )
    except InvalidTicketStatusError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "reason": "invalid_ticket_status",
                "ticket_id": exc.ticket_id,
                "status": exc.status,
            },
            status_code=409,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support ticket resolve error",
            exc,
            status_code=500,
            log_event="api_support_tickets_resolve",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_support_tickets_resolve")
    return JSONResponse({"ok": True, **result})
