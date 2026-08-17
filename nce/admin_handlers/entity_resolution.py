"""
Admin HTTP handlers for entity resolution (Wave 9: C1 dual surface).

Exports REST endpoints for entity resolution operations:
  - POST /api/admin/entity-resolution/resolve
  - GET  /api/admin/entity-resolution/queue
  - POST /api/admin/entity-resolution/queue/{queue_id}/confirm
  - POST /api/admin/entity-resolution/queue/{queue_id}/reject
"""

from __future__ import annotations

import json
import uuid

from nce.admin_handlers._shared import JSONResponse, admin_error_response, admin_state
from nce.auth import validate_agent_id
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.merge_queue import confirm, list_pending, reject
from nce.entity_resolution.resolver import resolve


async def api_entity_resolution_resolve(request) -> JSONResponse:
    """POST /api/admin/entity-resolution/resolve

    Rank and score existing kg_nodes against a candidate.

    Request body (JSON):
        namespace_id (str):     Required. UUID of the target namespace.
        candidate (dict):       Required. Raw entity data to match.
        keys (list[str]):       Required. Key names to compare.
        node_type (str):        Required. Entity type to filter on.

    Response (JSON):
        {
            "status": "ok",
            "matches": [
                {
                    "node_id": UUID string,
                    "score": float in [0, 1],
                    "matched_on": [list of key names]
                },
                ...
            ]
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    namespace_id = body.get("namespace_id")
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required field: namespace_id"},
            status_code=422,
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as e:
        return JSONResponse(
            {"error": f"Invalid namespace_id: {str(e)}"},
            status_code=422,
        )

    candidate = dict(body.get("candidate") or {})
    keys = list(body.get("keys") or [])
    node_type = body.get("node_type", "")

    if not node_type:
        return JSONResponse(
            {"error": "Missing required field: node_type"},
            status_code=422,
        )
    if not keys:
        return JSONResponse(
            {"error": "Missing required field: keys"},
            status_code=422,
        )

    try:
        ns_uuid = uuid.UUID(namespace_id)
        async with scoped_pg_session(admin_state.engine.pg_pool, ns_uuid) as conn:
            matches = await resolve(
                conn,
                namespace_id=ns_uuid,
                candidate=candidate,
                keys=keys,
                node_type=node_type,
            )

        return JSONResponse(
            {
                "status": "ok",
                "matches": [
                    {
                        "node_id": str(m.node_id),
                        "score": m.score,
                        "matched_on": m.matched_on,
                    }
                    for m in matches
                ],
            }
        )
    except ValueError as e:
        return JSONResponse(
            {"error": f"Invalid arguments: {str(e)}"},
            status_code=422,
        )
    except Exception as e:
        return admin_error_response(
            "Entity resolution error",
            e,
            status_code=500,
            log_event="api_entity_resolution_resolve",
        )


async def api_entity_resolution_queue_list(request) -> JSONResponse:
    """GET /api/admin/entity-resolution/queue

    List all pending rows in entity_merge_queue for a namespace.

    Query parameters:
        namespace_id (str):     Required. UUID of the target namespace.

    Response (JSON):
        {
            "status": "ok",
            "pending": [
                {
                    "id": UUID string,
                    "node_type": str,
                    "candidate_payload": dict,
                    "target_node_id": UUID string or null,
                    "score": float,
                    "status": "pending",
                    "created_at": ISO 8601 datetime string
                },
                ...
            ]
        }
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id = request.query_params.get("namespace_id")
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required parameter: namespace_id"},
            status_code=422,
        )

    try:
        validate_agent_id(namespace_id)
    except ValueError as e:
        return JSONResponse(
            {"error": f"Invalid namespace_id: {str(e)}"},
            status_code=422,
        )

    try:
        ns_uuid = uuid.UUID(namespace_id)
        async with scoped_pg_session(admin_state.engine.pg_pool, ns_uuid) as conn:
            rows = await list_pending(conn, namespace_id=ns_uuid)

        return JSONResponse(
            {
                "status": "ok",
                "pending": [
                    {
                        "id": str(row["id"]),
                        "node_type": row["node_type"],
                        "candidate_payload": row["candidate_payload"],
                        "target_node_id": str(row["target_node_id"])
                        if row["target_node_id"]
                        else None,
                        "score": float(row["score"]),
                        "status": row["status"],
                        "created_at": row["created_at"].isoformat(),
                    }
                    for row in rows
                ],
            }
        )
    except ValueError as e:
        return JSONResponse(
            {"error": f"Invalid arguments: {str(e)}"},
            status_code=422,
        )
    except Exception as e:
        return admin_error_response(
            "Merge queue list error",
            e,
            status_code=500,
            log_event="api_entity_resolution_queue_list",
        )


async def api_entity_resolution_queue_confirm(request) -> JSONResponse:
    """POST /api/admin/entity-resolution/queue/{queue_id}/confirm

    Mark a queue row as confirmed.

    Path parameters:
        queue_id (str):         UUID of the queue row to confirm.

    Request body (JSON):
        namespace_id (str):     Required. UUID of the target namespace.
        decided_by (str):       Required. Identifier of the decider.

    Response (JSON):
        {
            "status": "ok",
            "queue_id": UUID string
        }

    NO-AUTO-MERGE: This function marks status as "confirmed" **only** and never
    touches kg_nodes or kg_edges.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    queue_id_str = request.path_params.get("queue_id")
    if not queue_id_str:
        return JSONResponse(
            {"error": "Missing path parameter: queue_id"},
            status_code=422,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    namespace_id = body.get("namespace_id")
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required field: namespace_id"},
            status_code=422,
        )

    decided_by = body.get("decided_by")
    if not decided_by:
        return JSONResponse(
            {"error": "Missing required field: decided_by"},
            status_code=422,
        )

    try:
        validate_agent_id(namespace_id)
        queue_id = uuid.UUID(queue_id_str)
        ns_uuid = uuid.UUID(namespace_id)
    except ValueError as e:
        return JSONResponse(
            {"error": f"Invalid arguments: {str(e)}"},
            status_code=422,
        )

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, ns_uuid) as conn:
            await confirm(
                conn,
                namespace_id=ns_uuid,
                queue_id=queue_id,
                decided_by=decided_by,
            )

        return JSONResponse(
            {
                "status": "ok",
                "queue_id": str(queue_id),
            }
        )
    except LookupError as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=404,
        )
    except Exception as e:
        return admin_error_response(
            "Merge queue confirm error",
            e,
            status_code=500,
            log_event="api_entity_resolution_queue_confirm",
        )


async def api_entity_resolution_queue_reject(request) -> JSONResponse:
    """POST /api/admin/entity-resolution/queue/{queue_id}/reject

    Mark a queue row as rejected.

    Path parameters:
        queue_id (str):         UUID of the queue row to reject.

    Request body (JSON):
        namespace_id (str):     Required. UUID of the target namespace.
        decided_by (str):       Required. Identifier of the decider.

    Response (JSON):
        {
            "status": "ok",
            "queue_id": UUID string
        }

    NO-AUTO-MERGE: This function marks status as "rejected" **only** and never
    touches kg_nodes or kg_edges.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    queue_id_str = request.path_params.get("queue_id")
    if not queue_id_str:
        return JSONResponse(
            {"error": "Missing path parameter: queue_id"},
            status_code=422,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

    namespace_id = body.get("namespace_id")
    if not namespace_id:
        return JSONResponse(
            {"error": "Missing required field: namespace_id"},
            status_code=422,
        )

    decided_by = body.get("decided_by")
    if not decided_by:
        return JSONResponse(
            {"error": "Missing required field: decided_by"},
            status_code=422,
        )

    try:
        validate_agent_id(namespace_id)
        queue_id = uuid.UUID(queue_id_str)
        ns_uuid = uuid.UUID(namespace_id)
    except ValueError as e:
        return JSONResponse(
            {"error": f"Invalid arguments: {str(e)}"},
            status_code=422,
        )

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, ns_uuid) as conn:
            await reject(
                conn,
                namespace_id=ns_uuid,
                queue_id=queue_id,
                decided_by=decided_by,
            )

        return JSONResponse(
            {
                "status": "ok",
                "queue_id": str(queue_id),
            }
        )
    except LookupError as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=404,
        )
    except Exception as e:
        return admin_error_response(
            "Merge queue reject error",
            e,
            status_code=500,
            log_event="api_entity_resolution_queue_reject",
        )
