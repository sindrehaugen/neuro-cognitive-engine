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
  ``api_support_tickets_triage``   — POST /api/support/tickets/{id}/triage
  ``api_support_touchpoints_record`` — POST /api/support/touchpoints
  ``api_support_tickets_dispatch`` — POST /api/support/tickets/{id}/dispatch
  ``api_support_sync_now``         — POST /api/support/sync/now
  ``api_support_sync_status``      — GET  /api/support/sync/status

All handlers are thin REST wrappers over the vertical module cores in
``nce/vertical_modules/support/**`` (``do_open_ticket``, ``do_query_ticket``,
``do_sla_clock``, ``do_health_score``, ``do_troubleshoot``, ``do_resolve_ticket``,
``do_triage_ticket``, ``do_record_touchpoint``, ``do_dispatch_work_order``,
``do_sync_now``, ``do_sync_status``)
— adhering to the "one core function, two surfaces" pattern.

Mutating routes invalidate the MCP response cache via ``bump_mcp_cache_generation``.

Error mapping:
  missing/invalid ``namespace_id`` or path ``id``    -> 422
  ``ValueError`` from a core                         -> 422
  support vertical not enabled                       -> 409
  ticket absent (GET/resolve/troubleshoot/dispatch)  -> 404
  ticket already resolved / invalid status           -> 409
  dispatch ceiling exceeded                          -> 409
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
from nce.vertical_modules.support.dispatch import (
    DispatchCeilingExceededError,
    do_dispatch_work_order,
)
from nce.vertical_modules.support.health import do_health_score, do_record_touchpoint
from nce.vertical_modules.support.sla import do_sla_clock
from nce.vertical_modules.support.sync import do_sync_now, do_sync_status
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketAlreadyResolvedError,
    TicketNotFoundError,
    do_open_ticket,
    do_query_ticket,
    do_resolve_ticket,
)
from nce.vertical_modules.support.triage import do_triage_ticket
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


# ---------------------------------------------------------------------------
# POST /api/support/tickets/{id}/triage
# ---------------------------------------------------------------------------


async def api_support_tickets_triage(request: Any) -> JSONResponse:
    """POST /api/support/tickets/{id}/triage — triage ticket priority and routing.

    Path parameter:
        id (str, optional): Ticket UUID.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        ticket_id (str, optional): Ticket UUID if not provided in path.

    Response (JSON):
        {"ok": True, "ticket_id": str, "recommended_priority": str, ...}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ticket_id = (request.path_params.get("id") or "").strip()

    try:
        body = await request.json()
    except Exception:
        body = {}

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    if not ticket_id:
        ticket_id = str(body.get("ticket_id") or "").strip()
    if not ticket_id:
        return JSONResponse({"error": "Missing ticket id (in path or body)"}, status_code=422)

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id
    params["ticket_id"] = ticket_id

    try:
        result = await do_triage_ticket(admin_state.engine, params)
    except TicketNotFoundError as exc:
        return JSONResponse(
            {"error": str(exc), "not_found": True, "ticket_id": exc.ticket_id},
            status_code=404,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support ticket triage error",
            exc,
            status_code=500,
            log_event="api_support_tickets_triage",
        )

    return JSONResponse({"ok": True, **result})


# ---------------------------------------------------------------------------
# POST /api/support/touchpoints
# ---------------------------------------------------------------------------


async def api_support_touchpoints_record(request: Any) -> JSONResponse:
    """POST /api/support/touchpoints — record an ÉT-spørsmål touchpoint response.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        customer_id (str, required): Customer identifier.
        question_id (str, optional): Question identifier (default 'et_sporsmal_v1').
        answer (any, required): Customer answer / feedback.
        score (float, optional): Sentiment or rating score.

    Response (JSON):
        {"ok": True, "health": {...}}
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

    if not body.get("customer_id"):
        return JSONResponse({"error": "customer_id is required"}, status_code=422)
    if "answer" not in body:
        return JSONResponse({"error": "answer is required"}, status_code=422)

    pool = _extract_pool(admin_state.engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        return JSONResponse({"error": str(exc), "reason": "support_disabled"}, status_code=409)

    params = dict(body)
    params["namespace_id"] = namespace_id

    try:
        result = await do_record_touchpoint(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support touchpoint record error",
            exc,
            status_code=500,
            log_event="api_support_touchpoints_record",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_support_touchpoints_record")
    return JSONResponse({"ok": True, **result})


# ---------------------------------------------------------------------------
# POST /api/support/tickets/{id}/dispatch
# ---------------------------------------------------------------------------


async def api_support_tickets_dispatch(request: Any) -> JSONResponse:
    """POST /api/support/tickets/{id}/dispatch — dispatch ticket to Field Tech work order.

    Path parameter:
        id (str): Ticket UUID.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        estimated_cost (float, optional): Estimated cost to evaluate against DISPATCH_CEILING.
        dispatch_ceiling (float, optional): Ceiling override.
        confirm (bool, optional): Human confirmation override for over-ceiling dispatch.
        notes (str, optional): Dispatch notes.

    Response (JSON):
        {"ok": True, "dispatched": True, "ticket_id": str, "work_order_id": str, ...}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    ticket_id = (request.path_params.get("id") or "").strip()
    if not ticket_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    try:
        body = await request.json()
    except Exception:
        body = {}

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
        result = await do_dispatch_work_order(admin_state.engine, params)
    except DispatchCeilingExceededError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "reason": "dispatch_ceiling_exceeded",
                "estimated_cost": exc.estimated_cost,
                "ceiling": exc.ceiling,
            },
            status_code=409,
        )
    except TicketNotFoundError as exc:
        return JSONResponse(
            {"error": str(exc), "not_found": True, "ticket_id": exc.ticket_id},
            status_code=404,
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
            "Support ticket dispatch error",
            exc,
            status_code=500,
            log_event="api_support_tickets_dispatch",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_support_tickets_dispatch")
    return JSONResponse({"ok": True, **result})


# ---------------------------------------------------------------------------
# POST /api/support/sync/now (and /api/support/sync-now)
# ---------------------------------------------------------------------------


async def api_support_sync_now(request: Any) -> JSONResponse:
    """POST /api/support/sync/now — trigger incremental D365 case sync and proactive sweep.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        mode (str, optional): 'd365', 'both', or 'nce'.
        run_proactive_sweep (bool, optional): whether to run proactive sweep (default True).

    Response (JSON):
        {"ok": True, "status": "completed", "d365_sync": {...}, "proactive_sweep": {...}}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

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
        result = await do_sync_now(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support sync now error",
            exc,
            status_code=500,
            log_event="api_support_sync_now",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_support_sync_now")
    return JSONResponse({"ok": True, **result})


# ---------------------------------------------------------------------------
# GET /api/support/sync/status (and /api/support/sync-status)
# ---------------------------------------------------------------------------


async def api_support_sync_status(request: Any) -> JSONResponse:
    """GET /api/support/sync/status — check D365 feed health and sync status.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        mode (str, optional): data source mode.

    Response (JSON):
        {"ok": True, "status": "healthy", "last_sync": str, "source_mode": str}
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

    params = {
        "namespace_id": namespace_id,
        "mode": request.query_params.get("mode", "both"),
    }

    try:
        result = await do_sync_status(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Support sync status error",
            exc,
            status_code=500,
            log_event="api_support_sync_status",
        )

    return JSONResponse({"ok": True, **result})
