"""
nce/admin_handlers/webhooks.py

C4 Outbound Webhooks (Wave A-7)
Admin HTTP handlers for registering, listing, and removing outbound webhook subscriptions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from nce import admin_state
from nce.admin_handlers._shared import (
    _MISSING_NAMESPACE_FIELD,
    _MISSING_NAMESPACE_QUERY_PARAM,
    _require_namespace_id,
    admin_error_response,
    bump_mcp_cache_generation,
    logger,
)
from nce.outbound_webhooks import (
    create_webhook,
    delete_webhook,
    list_webhooks,
)


async def api_admin_webhooks_post(request: Request) -> JSONResponse:
    """POST /api/admin/webhooks

    Register a new outbound webhook for a namespace.

    JSON Body:
      namespace_id   (str, UUID, required)
      url            (str, required)
      secret         (str, required HMAC secret)
      selectors      (list[str], optional, default ["*"])
      is_active      (bool, optional, default True)
      description    (str, optional, default "")
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body: dict[str, Any] = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    namespace_id, err = _require_namespace_id(
        body.get("namespace_id"), missing_error=_MISSING_NAMESPACE_FIELD
    )
    if err:
        return err

    url = str(body.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "Missing required field: url"}, status_code=422)

    secret = str(body.get("secret") or "").strip()
    if not secret:
        return JSONResponse({"error": "Missing required field: secret"}, status_code=422)

    selectors_raw = body.get("selectors")
    if selectors_raw is not None and not isinstance(selectors_raw, (list, tuple)):
        return JSONResponse(
            {"error": "Field 'selectors' must be a list of strings"}, status_code=422
        )

    selectors = list(selectors_raw) if selectors_raw is not None else ["*"]
    is_active = bool(body.get("is_active", True))
    description = str(body.get("description") or "").strip()

    try:
        ns_uuid = uuid.UUID(namespace_id)
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            webhook = await create_webhook(
                conn,
                namespace_id=ns_uuid,
                url=url,
                secret=secret,
                selectors=selectors,
                is_active=is_active,
                description=description,
            )
    except Exception as exc:
        logger.exception("[admin_webhooks] failed to create webhook")
        return admin_error_response("Failed to create webhook", exc, status_code=500)

    await bump_mcp_cache_generation(admin_state.engine, route="api_admin_webhooks_post")
    return JSONResponse(webhook.to_dict(include_secret=False), status_code=201)


async def api_admin_webhooks_get(request: Request) -> JSONResponse:
    """GET /api/admin/webhooks?namespace_id=<uuid>[&is_active=true|false]

    List outbound webhooks registered for the given namespace.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    qp = request.query_params
    namespace_id, err = _require_namespace_id(
        qp.get("namespace_id"), missing_error=_MISSING_NAMESPACE_QUERY_PARAM
    )
    if err:
        return err

    is_active_raw = qp.get("is_active")
    is_active: bool | None = None
    if is_active_raw is not None:
        is_active = is_active_raw.lower() in ("true", "1", "yes")

    try:
        ns_uuid = uuid.UUID(namespace_id)
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            webhooks = await list_webhooks(conn, namespace_id=ns_uuid, is_active=is_active)
    except Exception as exc:
        logger.exception("[admin_webhooks] failed to list webhooks")
        return admin_error_response("Failed to list webhooks", exc, status_code=500)

    items = [w.to_dict(include_secret=False) for w in webhooks]
    return JSONResponse({"webhooks": items, "total": len(items)})


async def api_admin_webhooks_delete(request: Request) -> JSONResponse:
    """DELETE /api/admin/webhooks/{id}?namespace_id=<uuid>

    Delete an outbound webhook by ID.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_id = request.path_params.get("id", "").strip()
    try:
        webhook_id = uuid.UUID(raw_id)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": f"Invalid webhook id: {exc}"}, status_code=422)

    # Namespace can come from query params or body
    raw_ns = request.query_params.get("namespace_id")
    if not raw_ns and request.headers.get("content-type") == "application/json":
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw_ns = body.get("namespace_id")
        except Exception:
            pass

    namespace_id, err = _require_namespace_id(raw_ns, missing_error=_MISSING_NAMESPACE_QUERY_PARAM)
    if err:
        return err

    try:
        ns_uuid = uuid.UUID(namespace_id)
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            deleted = await delete_webhook(conn, namespace_id=ns_uuid, webhook_id=webhook_id)
    except Exception as exc:
        logger.exception("[admin_webhooks] failed to delete webhook")
        return admin_error_response("Failed to delete webhook", exc, status_code=500)

    if not deleted:
        return JSONResponse(
            {"error": "Webhook not found", "id": str(webhook_id)},
            status_code=404,
        )

    await bump_mcp_cache_generation(admin_state.engine, route="api_admin_webhooks_delete")
    return JSONResponse({"id": str(webhook_id), "deleted": True})
