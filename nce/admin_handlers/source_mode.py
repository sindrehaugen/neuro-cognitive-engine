"""
Admin HTTP handlers for the generic C5 source-mode / flip-gate surface.

Exports:
  ``api_source_mode_get`` — GET /api/admin/source-mode
  ``api_source_mode_put`` — PUT /api/admin/source-mode
"""

from __future__ import annotations

import uuid
from typing import Any

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


async def get_source_modes(pg_pool: Any, namespace_id: str, engine: str) -> JSONResponse:
    """Core GET logic for configured source modes."""
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
    pg_pool: Any,
    namespace_id: str,
    engine: str,
    func_name: str,
    mode: str,
    window_seconds: float | None = None,
) -> JSONResponse:
    """Core PUT logic for source-mode configuration."""
    if err_resp := _validate_namespace_and_engine(namespace_id, engine):
        return err_resp
    if not func_name:
        return admin_client_error("Missing required field: function", status_code=422)
    if mode not in ("d365", "both", "nce"):
        return admin_client_error(
            f"Invalid mode: {mode}. Must be one of ('d365', 'both', 'nce')",
            status_code=422,
        )

    ns_uuid = uuid.UUID(namespace_id)

    if mode == "nce":
        flip_kwargs: dict[str, Any] = {
            "namespace_id": ns_uuid,
            "engine": engine,
            "function": func_name,
        }
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


async def api_source_mode_get(request: Any) -> JSONResponse:
    """GET /api/admin/source-mode"""
    if err_resp := engine_unavailable():
        return err_resp

    namespace_id = str(request.query_params.get("namespace_id") or "").strip()
    engine = str(request.query_params.get("engine") or "").strip()
    assert admin_state.engine is not None
    return await get_source_modes(admin_state.engine.pg_pool, namespace_id, engine)


async def api_source_mode_put(request: Any) -> JSONResponse:
    """PUT /api/admin/source-mode"""
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

    assert admin_state.engine is not None
    return await put_source_mode(admin_state.engine.pg_pool, namespace_id, engine, func_name, mode)
