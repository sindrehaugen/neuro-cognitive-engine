"""
MCP handlers for entity resolution (Wave 9: C1 dual surface).

Wraps the following Wave-5/6 core functions:
  - resolve() — fuzzy-match and rank candidates against existing kg_nodes
  - list_pending() — list all pending rows in entity_merge_queue
  - confirm() — mark a queue row as confirmed
  - reject() — mark a queue row as rejected

Each handler is a thin adapter: marshals arguments, runs the core function
inside a namespace-scoped session, and returns a JSON string. No new logic;
all domain invariants (never-auto-merge, no PII logging) are preserved from
the underlying Wave 5-6 functions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.merge_queue import confirm, list_pending, reject
from nce.entity_resolution.resolver import Match, resolve
from nce.mcp_args import require_namespace_id as _require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.orchestrator import NCEEngine


@mcp_handler
async def handle_resolve(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Rank and score existing kg_nodes against a candidate.

    Arguments:
        namespace_id (str):     Required. UUID of the target namespace.
        candidate (dict):       Required. Raw entity data to match.
        keys (list[str]):       Required. Key names to compare.
        node_type (str):        Required. Entity type to filter on.

    Returns (JSON):
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

    Matches are ranked highest-score first; empty list when no candidates match.
    """
    namespace_id = uuid.UUID(_require_namespace_id(arguments))
    candidate = dict(arguments.get("candidate") or {})
    keys = list(arguments.get("keys") or [])
    node_type = arguments.get("node_type", "")

    if not node_type:
        raise ValueError("Missing required argument: node_type")
    if not keys:
        raise ValueError("Missing required argument: keys")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        matches: list[Match] = await resolve(
            conn,
            namespace_id=namespace_id,
            candidate=candidate,
            keys=keys,
            node_type=node_type,
        )

    return json.dumps(
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


@mcp_handler
async def handle_merge_queue_list(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """List all pending rows in entity_merge_queue for a namespace.

    Arguments:
        namespace_id (str):     Required. UUID of the target namespace.

    Returns (JSON):
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

    Rows are ordered oldest-first (created_at ASC) so reviewers work through
    the backlog in arrival order.
    """
    namespace_id = uuid.UUID(_require_namespace_id(arguments))

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        rows = await list_pending(conn, namespace_id=namespace_id)

    return json.dumps(
        {
            "status": "ok",
            "pending": [
                {
                    "id": str(row["id"]),
                    "node_type": row["node_type"],
                    "candidate_payload": row["candidate_payload"],
                    "target_node_id": str(row["target_node_id"]) if row["target_node_id"] else None,
                    "score": float(row["score"]),
                    "status": row["status"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ],
        }
    )


@mcp_handler
async def handle_merge_queue_confirm(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Mark a queue row as confirmed.

    Arguments:
        namespace_id (str):     Required. UUID of the target namespace.
        queue_id (str):         Required. UUID of the queue row to confirm.
        decided_by (str):       Required. Identifier of the decider.

    Returns (JSON):
        {
            "status": "ok",
            "queue_id": UUID string
        }

    NO-AUTO-MERGE: This function marks status as "confirmed" **only** and never
    touches kg_nodes or kg_edges. Node survivorship is Wave 7.

    Raises:
        ValueError: If the queue row does not exist or is not in pending status.
    """
    namespace_id = uuid.UUID(_require_namespace_id(arguments))
    queue_id_str = arguments.get("queue_id")
    decided_by = arguments.get("decided_by")

    if not queue_id_str:
        raise ValueError("Missing required argument: queue_id")
    if not decided_by:
        raise ValueError("Missing required argument: decided_by")

    queue_id = uuid.UUID(queue_id_str)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        await confirm(
            conn,
            namespace_id=namespace_id,
            queue_id=queue_id,
            decided_by=decided_by,
        )

    return json.dumps(
        {
            "status": "ok",
            "queue_id": str(queue_id),
        }
    )


@mcp_handler
async def handle_merge_queue_reject(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Mark a queue row as rejected.

    Arguments:
        namespace_id (str):     Required. UUID of the target namespace.
        queue_id (str):         Required. UUID of the queue row to reject.
        decided_by (str):       Required. Identifier of the decider.

    Returns (JSON):
        {
            "status": "ok",
            "queue_id": UUID string
        }

    NO-AUTO-MERGE: This function marks status as "rejected" **only** and never
    touches kg_nodes or kg_edges.

    Raises:
        ValueError: If the queue row does not exist or is not in pending status.
    """
    namespace_id = uuid.UUID(_require_namespace_id(arguments))
    queue_id_str = arguments.get("queue_id")
    decided_by = arguments.get("decided_by")

    if not queue_id_str:
        raise ValueError("Missing required argument: queue_id")
    if not decided_by:
        raise ValueError("Missing required argument: decided_by")

    queue_id = uuid.UUID(queue_id_str)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        await reject(
            conn,
            namespace_id=namespace_id,
            queue_id=queue_id,
            decided_by=decided_by,
        )

    return json.dumps(
        {
            "status": "ok",
            "queue_id": str(queue_id),
        }
    )
