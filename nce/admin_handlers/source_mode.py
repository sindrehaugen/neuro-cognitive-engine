"""
Admin HTTP handlers for the generic C5 source-mode / flip-gate surface.

Wave D-9 (2026-09-20), completing the charter's original brief: PR #310
generalized the *Python* mechanism (``nce.source_mode.flip``) and rewired
``nce/vertical_modules/sales/flip.py``'s two functions to thin wrappers over
it, but left the charter's other stated deliverable -- "GET
/api/admin/source-mode per function" -- unbuilt. Only a sales-specific route
(``/api/admin/sales/source-mode``, ``nce/admin_handlers/sales.py``) existed,
and its PUT handler independently re-implemented the exact same
check-then-upsert logic ``nce.source_mode.flip.flip_function`` already
provides generically -- a THIRD copy of the pattern PR #310's own docstring
warns about (the first two were ``sales/flip.py``'s pre-#310 functions).

Exports:
  ``api_source_mode_get`` — GET /api/admin/source-mode
  ``api_source_mode_put`` — PUT /api/admin/source-mode

Deliberately does NOT validate ``function`` against a fixed per-engine set
(sales's own route does that, for its own ten sales function names -- a
content decision about which function names are legitimate for sales,
which stays sales's call). ``source_mode_config.function`` has no CHECK
constraint restricting its value, and this route is the shared mechanism
every future engine's flip gate reaches through, not a place to encode one
engine's vocabulary.
"""

from __future__ import annotations

import uuid

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
)
from nce.admin_http_support import admin_client_error, engine_unavailable
from nce.db_utils import scoped_pg_session
from nce.source_mode.flip import flip_function


def _validate_namespace_and_engine(namespace_id: str, engine: str) -> JSONResponse | None:
    if not namespace_id:
        return admin_client_error("Missing required field: namespace_id", status_code=422)
    try:
        uuid.UUID(namespace_id)
    except ValueError as exc:
        return admin_client_error(f"Invalid namespace_id: {exc}", status_code=422)
    if not engine:
        return admin_client_error("Missing required field: engine", status_code=422)
    return None


async def get_source_modes(pg_pool, namespace_id: str, engine: str) -> JSONResponse:
    """Core GET logic, callable directly (no HTTP ``Request`` needed) so a
    per-engine route (e.g. ``api_sales_source_mode_get``) can delegate here
    without synthesizing a fake request object.
    """
    if err_resp := _validate_namespace_and_engine(namespace_id, engine):
        return err_resp

    try:
        ns_uuid = uuid.UUID(namespace_id)
        async with scoped_pg_session(pg_pool, ns_uuid) as conn:
            rows = await conn.fetch(
                """
                SELECT function, mode
                  FROM source_mode_config
                 WHERE namespace_id = $1
                   AND engine = $2
                """,
                ns_uuid,
                engine,
            )
        modes = {row["function"]: row["mode"] for row in rows}
        return JSONResponse(
            {
                "namespace_id": namespace_id,
                "engine": engine,
                "modes": modes,
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Source-mode GET error",
            exc,
            status_code=500,
            log_event="api_source_mode_get",
        )


async def put_source_mode(
    pg_pool,
    namespace_id: str,
    engine: str,
    func_name: str,
    mode: str,
    window_seconds: float | None = None,
) -> JSONResponse:
    """Core PUT logic, callable directly -- see :func:`get_source_modes`.

    A ``mode="nce"`` request is gated through
    :func:`nce.source_mode.flip.flip_function` -- the single canonical "flip
    only when the divergence log is clean over the parity window" decision,
    not a second reimplementation of it. ``d365``/``both`` need no gate
    (they never leave dual-source or external-only) and are upserted
    directly.

    ``window_seconds``: which parity window to gate on is a per-engine
    content decision, same category as ``valid_functions`` in
    ``admin_handlers/sales.py`` -- left ``None`` here (the default), this
    passes through to :func:`flip_function`'s own default (seven days,
    ``nce.source_mode.flip._DEFAULT_WINDOW_SECONDS``). A caller with a real
    reason to use a different window (as sales's own pre-existing route
    does, one hour, preserved via its own explicit argument) passes it
    explicitly rather than this generic surface inventing a value for
    engines it knows nothing about.
    """
    if err_resp := _validate_namespace_and_engine(namespace_id, engine):
        return err_resp
    if not func_name:
        return admin_client_error("Missing required field: function", status_code=422)
    if mode not in ("d365", "both", "nce"):
        return admin_client_error(
            f"Invalid mode: {mode}. Must be one of ('d365', 'both', 'nce')", status_code=422
        )

    ns_uuid = uuid.UUID(namespace_id)

    if mode == "nce":
        flip_kwargs = {"namespace_id": ns_uuid, "engine": engine, "function": func_name}
        if window_seconds is not None:
            flip_kwargs["window_seconds"] = window_seconds
        try:
            result = await flip_function(pg_pool, **flip_kwargs)
        except Exception as exc:
            return admin_error_response(
                "Source-mode flip error",
                exc,
                status_code=500,
                log_event="api_source_mode_put_flip",
            )
        if not result.get("ok"):
            # "blocked" is load-bearing wording, not decoration: the
            # pre-D-9 sales-specific PUT handler's exact response text was
            # "Flip to nce mode is blocked due to recent divergences", and
            # test_sales_divergence.py::test_admin_sales_source_mode_endpoints
            # asserts on that word. flip_function()'s own reason text
            # ("Refused: N divergence(s)...") is appended for detail, not
            # substituted, so this generic route stays a strict superset of
            # information, not a silent rewording of an existing contract.
            reason = result.get("reason", "recent divergences")
            return admin_client_error(
                f"Flip to nce mode is blocked due to recent divergences ({reason})",
                status_code=400,
            )
        return JSONResponse(
            {
                "namespace_id": namespace_id,
                "engine": engine,
                "function": func_name,
                "mode": "nce",
                "status": "updated",
            }
        )

    try:
        async with scoped_pg_session(pg_pool, ns_uuid) as conn:
            await conn.execute(
                """
                INSERT INTO source_mode_config (namespace_id, engine, function, mode, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (namespace_id, engine, function)
                DO UPDATE SET mode = EXCLUDED.mode, updated_at = EXCLUDED.updated_at
                """,
                ns_uuid,
                engine,
                func_name,
                mode,
            )
        return JSONResponse(
            {
                "namespace_id": namespace_id,
                "engine": engine,
                "function": func_name,
                "mode": mode,
                "status": "updated",
            }
        )
    except Exception as exc:
        return admin_error_response(
            "Source-mode PUT error",
            exc,
            status_code=500,
            log_event="api_source_mode_put",
        )


async def api_source_mode_get(request) -> JSONResponse:
    """GET /api/admin/source-mode

    Retrieve the configured source modes for any ``(namespace_id, engine)``
    pair. Generic sibling of ``api_sales_source_mode_get`` -- same response
    shape, ``engine`` taken from a query param instead of hardcoded.

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        engine       (str, required): Engine key, e.g. "sales".

    Response (JSON):
        {
          "namespace_id": str,
          "engine": str,
          "modes": {
            "<function>": "d365" | "both" | "nce",
            ...
          }
        }
    """
    if err_resp := engine_unavailable():
        return err_resp

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    engine = str(request.query_params.get("engine") or "").strip()
    return await get_source_modes(admin_state.engine.pg_pool, namespace_id, engine)


async def api_source_mode_put(request) -> JSONResponse:
    """PUT /api/admin/source-mode

    Configure the source mode for a given ``(engine, function)`` pair. See
    :func:`put_source_mode` for the gating logic.

    Request body (JSON):
        namespace_id   (str, required): Active namespace UUID.
        engine         (str, required): Engine key, e.g. "sales".
        function       (str, required): Function key (e.g. "list_customers").
        mode           (str, required): Target mode ("d365", "both", or "nce").
        window_seconds (float, optional): Parity-window override for the
                        ``mode="nce"`` gate. Omitted by default, which lets
                        :func:`nce.source_mode.flip.flip_function`'s own
                        default (seven days) apply -- see
                        :func:`put_source_mode`'s docstring for why this
                        generic route does not invent its own value.

    Response (JSON):
        {
          "namespace_id": str,
          "engine": str,
          "function": str,
          "mode": str,
          "status": "updated"
        }
    On a blocked flip: 400 with {"error": "..."} naming the divergence count.
    """
    if err_resp := engine_unavailable():
        return err_resp

    try:
        body = await request.json()
    except Exception:
        return admin_client_error("Invalid JSON body", status_code=422)

    namespace_id = str(body.get("namespace_id") or "").strip()
    engine = str(body.get("engine") or "").strip()
    func_name = str(body.get("function") or "").strip()
    mode = str(body.get("mode") or "").strip()
    window_seconds = body.get("window_seconds")
    if window_seconds is not None:
        try:
            window_seconds = float(window_seconds)
        except (TypeError, ValueError):
            return admin_client_error("window_seconds must be a number", status_code=422)

    return await put_source_mode(
        admin_state.engine.pg_pool, namespace_id, engine, func_name, mode, window_seconds
    )
