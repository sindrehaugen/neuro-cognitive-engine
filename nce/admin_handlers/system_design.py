"""
Admin HTTP handlers for the System Design vertical module (W11: Lucid export).

Exports:
  ``api_system_design_publish_design_docs``
      POST /api/system-design/publish-design-docs

Thin REST wrapper — delegates to ``do_publish_design_docs`` (lucid.py).
EXPORT ONLY — Lucid import is cut (spec correction, Wave 11).
No mutation of domain state; the only side-effect is the outbound Lucid API call.
"""

from __future__ import annotations

import logging

from nce.admin_handlers._shared import (
    JSONResponse,
    _require_namespace_id,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
)
from nce.vertical_modules.system_design.lucid import do_publish_design_docs

log = logging.getLogger("nce.admin_handlers.system_design")


async def api_system_design_publish_design_docs(request) -> JSONResponse:
    """POST /api/system-design/publish-design-docs

    JSON body:
        namespace_id (str, required): Active namespace UUID.
        design_id    (str, required): Design identifier to export.

    Response (JSON):
        {"status": "ok", "lucid_url": str | null}

    Returns ``lucid_url: null`` when Lucid credentials are unset (clean no-op).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    design_id = str(body.get("design_id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing required field: design_id"}, status_code=422)

    try:
        result = await do_publish_design_docs(
            admin_state.engine,
            {"namespace_id": namespace_id, "design_id": design_id},
        )
    except Exception as exc:
        log.exception("api_system_design_publish_design_docs: unexpected error")
        return admin_error_response(exc, status_code=500)

    # Mirror the MCP dispatch loop's post-mutation invalidation
    # (system_design_publish_design_docs is a mutation=True tool).
    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_publish_design_docs"
    )

    return JSONResponse({"status": "ok", "lucid_url": result.get("lucid_url")})
