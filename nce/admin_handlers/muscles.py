from __future__ import annotations

import json
import uuid

from starlette.responses import JSONResponse

from nce import admin_state
from nce.admin_handlers._shared import (
    admin_error_response,
    admin_validation_error,
    parse_optional_uuid,
)
from nce.models import ActorTrustOut, ApprovalQueueItemOut


async def api_admin_actor_trust(request):
    """GET /api/admin/actor-trust
    Query params: namespace_id?
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    qp = request.query_params
    try:
        namespace_id = parse_optional_uuid(qp.get("namespace_id"))
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where = []
    args = []
    if namespace_id:
        where.append("namespace_id = $1")
        args.append(namespace_id)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    query = f"""
        SELECT namespace_id, actor_id, actor_kind, confirmations, rejections, contradictions_sourced, trust, updated_at
        FROM actor_trust
        {where_sql}
        ORDER BY namespace_id, actor_id
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            rows = await conn.fetch(query, *args)
    except Exception as exc:
        return admin_error_response("Failed to query actor trust", exc, status_code=500)

    items = [ActorTrustOut.model_validate(dict(r)).model_dump(mode="json") for r in rows]
    return JSONResponse(items)


async def api_admin_approval_queue_list(request):
    """GET /api/admin/approval-queue
    Query params: namespace_id?, status?
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    qp = request.query_params
    try:
        namespace_id = parse_optional_uuid(qp.get("namespace_id"))
        status = qp.get("status")
        if status and status not in ("pending", "approved", "rejected", "executed", "expired"):
            return JSONResponse(
                {"error": "status must be pending|approved|rejected|executed|expired"},
                status_code=422,
            )
    except ValueError as exc:
        return admin_validation_error(exc, status_code=422)

    where = []
    args = []
    i = 1
    if namespace_id:
        where.append(f"namespace_id = ${i}")
        args.append(namespace_id)
        i += 1
    if status:
        where.append(f"status = ${i}")
        args.append(status)
        i += 1

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    query = f"""
        SELECT id, namespace_id, agent_id, action_type, target_system, target_entity_id, proposed_payload, status, dry_run_result, created_at, resolved_at, resolved_by
        FROM action_approval_queue
        {where_sql}
        ORDER BY created_at DESC
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            rows = await conn.fetch(query, *args)
    except Exception as exc:
        return admin_error_response("Failed to query approval queue", exc, status_code=500)

    items = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("proposed_payload"), str):
            d["proposed_payload"] = json.loads(d["proposed_payload"])
        if isinstance(d.get("dry_run_result"), str):
            d["dry_run_result"] = json.loads(d["dry_run_result"])
        items.append(ApprovalQueueItemOut.model_validate(d).model_dump(mode="json"))

    return JSONResponse(items)


async def api_admin_approval_queue_get(request):
    """GET /api/admin/approval-queue/{id}"""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_id = request.path_params.get("id")
    try:
        item_id = uuid.UUID(raw_id) if raw_id else None
    except ValueError:
        return JSONResponse({"error": "Invalid approval queue item ID"}, status_code=422)

    if not item_id:
        return JSONResponse({"error": "id path parameter is required"}, status_code=422)

    query = """
        SELECT id, namespace_id, agent_id, action_type, target_system, target_entity_id, proposed_payload, status, dry_run_result, created_at, resolved_at, resolved_by
        FROM action_approval_queue
        WHERE id = $1
        LIMIT 1
    """

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            row = await conn.fetchrow(query, item_id)
    except Exception as exc:
        return admin_error_response("Failed to fetch approval queue item", exc, status_code=500)

    if not row:
        return JSONResponse({"error": f"Approval queue item {item_id} not found"}, status_code=404)

    d = dict(row)
    if isinstance(d.get("proposed_payload"), str):
        d["proposed_payload"] = json.loads(d["proposed_payload"])
    if isinstance(d.get("dry_run_result"), str):
        d["dry_run_result"] = json.loads(d["dry_run_result"])

    item = ApprovalQueueItemOut.model_validate(d).model_dump(mode="json")
    return JSONResponse(item)
