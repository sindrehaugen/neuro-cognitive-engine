"""
nce/admin_handlers/assets.py
==============================
Admin HTTP handlers for the Assets vertical module (Module 9, Wave 3 —
Batch 143, ``assets-surface``).

Exports:
  ``api_assets_get``               — GET  /api/assets/{id}
  ``api_assets_list``               — GET  /api/assets
  ``api_assets_advance_lifecycle``  — POST /api/assets/{id}/lifecycle

All three are thin REST wrappers over the Wave 3 cores in
``nce/vertical_modules/assets/mcp_handlers.py`` (``do_get_asset`` /
``do_list_assets`` / ``do_advance_lifecycle``) — the "one core function,
two surfaces" pattern (``docs/vertical_engines/VERTICAL_MODULE_PATTERN.md``,
"Dual-surface exposure"). They contain no business logic and no LLM in the
path.

Every ``do_*`` core already returns JSON-safe values (UUID/timestamp columns
are normalised to str/ISO-8601 inside ``_row_to_asset_dict``), so — unlike
``nce/admin_handlers/_shared.py``'s ``_json_safe`` (needed by inventory for
``Decimal`` quantities) — no extra serialisation pass is required here.

Error mapping:
  missing/invalid ``namespace_id`` or path ``id``  -> 422
  ``ValueError`` from a core (bad params)           -> 422
  asset absent (GET)                                -> 200, ``{"asset": null}``
                                                        (mirrors ``api_product_get``)
  asset absent (advance-lifecycle)                  -> 404
  illegal lifecycle transition (business refusal)   -> 409
  anything else                                     -> 500 via ``admin_error_response``
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
from nce.vertical_modules.assets.mcp_handlers import (
    do_advance_lifecycle,
    do_attach_sla,
    do_compute_health,
    do_get_asset,
    do_list_assets,
    do_pull_telemetry,
    do_seed_asset_from_bom,
)

log = logging.getLogger("nce.admin_handlers.assets")


# ---------------------------------------------------------------------------
# GET /api/assets/{id}
# ---------------------------------------------------------------------------


async def api_assets_get(request: Any) -> JSONResponse:
    """GET /api/assets/{id}

    Path parameter:
        id (str): the asset's UUID (``assets.id``).

    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"ok": True, "asset": {...} | None}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    asset_id = request.path_params.get("id", "").strip()
    if not asset_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    try:
        result = await do_get_asset(
            admin_state.engine, {"namespace_id": namespace_id, "asset_id": asset_id}
        )
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Assets get error",
            exc,
            status_code=500,
            log_event="api_assets_get",
        )


# ---------------------------------------------------------------------------
# GET /api/assets
# ---------------------------------------------------------------------------


async def api_assets_list(request: Any) -> JSONResponse:
    """GET /api/assets

    Query parameters:
        namespace_id            (str, required): Active namespace UUID.
        functional_location_id  (str, optional): filter to one room.
        lifecycle_state         (str, optional): filter to one lifecycle state.

    Response (JSON):
        {"ok": True, "items": [{...}, ...]}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    params: dict[str, Any] = {"namespace_id": namespace_id}
    functional_location_id = request.query_params.get("functional_location_id")
    if functional_location_id is not None:
        params["functional_location_id"] = functional_location_id
    lifecycle_state = request.query_params.get("lifecycle_state")
    if lifecycle_state is not None:
        params["lifecycle_state"] = lifecycle_state

    try:
        result = await do_list_assets(admin_state.engine, params)
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Assets list error",
            exc,
            status_code=500,
            log_event="api_assets_list",
        )


# ---------------------------------------------------------------------------
# POST /api/assets/{id}/lifecycle
# ---------------------------------------------------------------------------


async def api_assets_advance_lifecycle(request: Any) -> JSONResponse:
    """POST /api/assets/{id}/lifecycle

    Advance an asset to a new lifecycle state (the 14-state machine in
    ``nce/vertical_modules/assets/lifecycle.py``).

    Path parameter:
        id (str): the asset's UUID (``assets.id``).

    Request body (JSON):
        namespace_id  (str, required): Active namespace UUID.
        target_state  (str, required): e.g. ``"VERIFIED"``.

    Response (JSON) — success (incl. idempotent no-op):
        {"ok": True, "changed": bool, "asset_id", "previous_state",
         "new_state", "error": None}  HTTP 200
    Response (JSON) — asset absent:
        {"ok": False, "not_found": True, "asset_id", "error": str}  HTTP 404
    Response (JSON) — illegal transition (business refusal):
        {"ok": False, "changed": False, "asset_id", "previous_state",
         "new_state", "error": str}  HTTP 409
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    asset_id = request.path_params.get("id", "").strip()
    if not asset_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params = {
        "namespace_id": namespace_id,
        "asset_id": asset_id,
        "target_state": body.get("target_state", ""),
    }

    try:
        result = await do_advance_lifecycle(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Assets advance-lifecycle error",
            exc,
            status_code=500,
            log_event="api_assets_advance_lifecycle",
        )

    # The core committed (or refused) without raising; mirror the MCP dispatch
    # loop's post-mutation invalidation so cacheable reads (assets_get /
    # assets_list) cannot serve the pre-mutation row for MCP_CACHE_TTL_S.
    await bump_mcp_cache_generation(admin_state.engine, route="api_assets_advance_lifecycle")

    if result.get("not_found"):
        return JSONResponse(result, status_code=404)
    if result.get("ok"):
        return JSONResponse(result)
    return JSONResponse(result, status_code=409)


# ---------------------------------------------------------------------------
# POST /api/assets/seed-from-bom
# ---------------------------------------------------------------------------


async def api_assets_seed_from_bom(request: Any) -> JSONResponse:
    """POST /api/assets/seed-from-bom

    Seed an asset row from a BOM line (install handover entry point).

    Request body (JSON):
        namespace_id           (str, required): Active namespace UUID.
        bom_line_id            (str, required): BOM line identifier.
        serial                 (str, optional): Physical serial number.
        functional_location_id (str, optional): Room/location identifier.

    Response (JSON):
        {"ok": True, "created": bool, "asset_id": str, ...} HTTP 201 (created) or 200 (replayed)
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

    params = {
        "namespace_id": namespace_id,
        "bom_line_id": body.get("bom_line_id"),
        "serial": body.get("serial"),
        "functional_location_id": body.get("functional_location_id"),
    }

    try:
        result = await do_seed_asset_from_bom(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Assets seed-from-bom error",
            exc,
            status_code=500,
            log_event="api_assets_seed_from_bom",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_assets_seed_from_bom")
    status = 201 if result.get("created") else 200
    return JSONResponse(result, status_code=status)


# ---------------------------------------------------------------------------
# POST /api/assets/{id}/telemetry
# ---------------------------------------------------------------------------


async def api_assets_pull_telemetry(request: Any) -> JSONResponse:
    """POST /api/assets/{id}/telemetry

    Pull telemetry samples for an asset via its configured platform adapter.

    Path parameter:
        id (str): the asset's UUID.

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        platform     (str, optional): Telemetry platform (defaults to mock).

    Response (JSON):
        {"ok": True, "asset_id": str, "platform": str, "pulled": int, ...} HTTP 200
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    asset_id = request.path_params.get("id", "").strip()
    if not asset_id:
        return JSONResponse({"error": "Missing path parameter: id"}, status_code=422)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err

    params = {
        "namespace_id": namespace_id,
        "asset_id": asset_id,
        "platform": body.get("platform"),
    }

    try:
        result = await do_pull_telemetry(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Assets pull-telemetry error",
            exc,
            status_code=500,
            log_event="api_assets_pull_telemetry",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_assets_pull_telemetry")
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# POST /api/assets/sla/attach
# ---------------------------------------------------------------------------


async def api_assets_attach_sla(request: Any) -> JSONResponse:
    """POST /api/assets/sla/attach

    Attach room-level SLA coverage link to an agreement.

    Request body (JSON):
        namespace_id           (str, required): Active namespace UUID.
        agreement_id           (str, required): Agreement UUID.
        functional_location_id (str, required): Room/functional location ID.
        namespace_slug         (str, optional): Namespace slug.

    Response (JSON):
        {"status": "ok", "agreement_id": str, "functional_location_id": str, ...} HTTP 200
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

    params = {
        "namespace_id": namespace_id,
        "agreement_id": body.get("agreement_id"),
        "functional_location_id": body.get("functional_location_id"),
        "namespace_slug": body.get("namespace_slug"),
    }

    try:
        result = await do_attach_sla(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Assets attach-sla error",
            exc,
            status_code=500,
            log_event="api_assets_attach_sla",
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_assets_attach_sla")
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# GET /api/assets/{id}/health or /api/assets/health
# ---------------------------------------------------------------------------


async def api_assets_health(request: Any) -> JSONResponse:
    """GET /api/assets/{id}/health or /api/assets/health?asset_id=...

    Compute and return asset health score and coverage summary (Watcher).

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        asset_id     (str, optional if {id} in path): Asset UUID.

    Response (JSON):
        {"ok": True, "asset_id": str, "health_score": float, "coverage": str, ...}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    asset_id = (request.path_params.get("id") or request.query_params.get("asset_id") or "").strip()
    if not asset_id:
        return JSONResponse(
            {"error": "Missing required parameter: asset_id or id path parameter"},
            status_code=422,
        )

    namespace_id, err = _require_namespace_id(request.query_params.get("namespace_id"))
    if err is not None:
        return err

    try:
        result = await do_compute_health(
            admin_state.engine,
            {"namespace_id": namespace_id, "asset_id": asset_id},
        )
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Assets health error",
            exc,
            status_code=500,
            log_event="api_assets_health",
        )
