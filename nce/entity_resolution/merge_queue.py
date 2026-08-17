"""C1 merge-review queue API — enqueue, list, confirm, reject.

This module is the **only** write path for ``entity_merge_queue``.  It
intentionally does NOT merge nodes; that is Wave 7 (survivorship).

SCOPE LOCK — no-auto-merge invariant
-------------------------------------
``confirm`` and ``reject`` mutate **only** the queue row:
  - ``status``     ← "confirmed" | "rejected"
  - ``decided_by`` ← caller-supplied decider identifier
  - ``decided_at`` ← now()

Neither function touches ``kg_nodes`` or ``kg_edges``.  Any temptation
to do so here is a scope violation — STOP and report to the orchestrator.

Design rules (uncle-bob-craft / dependency rule):
  - Domain core: no web, HTTP, admin, or framework imports.
  - SRP: one function, one job (enqueue / list_pending / confirm / reject).
  - Caller holds the ``scoped_pg_session`` context; these functions accept
    an already-scoped ``conn`` plus an explicit ``namespace_id`` guard
    (belt-and-braces, per project convention).
  - Never log candidate PII at INFO level (Rule 8).
  - All writes carry their ``namespace_id`` filter; RLS enforces the same
    constraint at the database level.

Caller contract:
  ``conn`` must already have the RLS namespace GUC set (obtained via
  ``scoped_pg_session``).  The explicit ``namespace_id`` WHERE clause in
  every query is a redundant safety guard that keeps the invariant visible
  at the SQL level — identical to the pattern in ownership.py and resolver.py.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def enqueue(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    node_type: str,
    candidate: dict[str, Any],
    target: UUID | None,
    score: float,
) -> UUID:
    """Insert one pending row into ``entity_merge_queue`` and return its id.

    The caller is responsible for ensuring ``score`` represents a sub-threshold
    match that requires human review — this function does not enforce any
    threshold.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set
        (i.e. obtained via ``scoped_pg_session``).
    namespace_id:
        Active namespace UUID.
    node_type:
        The entity type of the candidate (e.g. ``'device'``).
    candidate:
        Raw payload dict for the entity being reviewed.  Stored as JSONB.
        Do NOT include PII fields that must not be persisted.
    target:
        UUID of the existing kg_nodes row this candidate may merge with,
        or ``None`` when no target node has been identified yet.
    score:
        Match similarity score in [0, 1] as produced by ``resolve()``.

    Returns
    -------
    UUID:
        The ``id`` of the newly created queue row.
    """
    ns_uuid = _to_uuid(namespace_id)
    row_id: UUID = await conn.fetchval(
        """
        INSERT INTO entity_merge_queue
            (namespace_id, node_type, candidate_payload, target_node_id, score, status)
        VALUES ($1, $2, $3::jsonb, $4, $5, 'pending')
        RETURNING id
        """,
        ns_uuid,
        node_type,
        json.dumps(candidate),
        target,
        score,
    )
    return row_id


async def list_pending(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
) -> list[dict[str, Any]]:
    """Return all pending rows in ``entity_merge_queue`` for the namespace.

    Rows are ordered oldest-first (``created_at ASC``) so reviewers work
    through the backlog in arrival order.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set.
    namespace_id:
        Active namespace UUID.

    Returns
    -------
    list[dict[str, Any]]:
        List of dicts with keys: ``id``, ``node_type``, ``candidate_payload``,
        ``target_node_id``, ``score``, ``status``, ``created_at``.
        Empty list when no pending rows exist.
    """
    ns_uuid = _to_uuid(namespace_id)
    rows = await conn.fetch(
        """
        SELECT id, node_type, candidate_payload, target_node_id, score, status, created_at
        FROM   entity_merge_queue
        WHERE  namespace_id = $1
          AND  status       = 'pending'
        ORDER BY created_at ASC
        """,
        ns_uuid,
    )
    return [dict(r) for r in rows]


async def confirm(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    queue_id: UUID,
    decided_by: str,
) -> None:
    """Mark a queue row as ``confirmed`` and record the decider.

    NO-AUTO-MERGE GUARD
    -------------------
    This function updates **only** the queue row (status, decided_by,
    decided_at).  It does NOT merge kg_nodes, does NOT write kg_edges,
    and does NOT modify any other table.  Node survivorship is Wave 7.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set.
    namespace_id:
        Active namespace UUID.  Explicit WHERE predicate in addition to RLS.
    queue_id:
        The ``id`` of the queue row to confirm.
    decided_by:
        Identifier of the human (or authorised service) recording the
        decision.  Must be non-empty.

    Raises
    ------
    LookupError:
        The queue row does not exist in this namespace or is not in
        ``pending`` status.
    """
    await _set_decision(
        conn,
        namespace_id=namespace_id,
        queue_id=queue_id,
        new_status="confirmed",
        decided_by=decided_by,
    )


async def reject(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    queue_id: UUID,
    decided_by: str,
) -> None:
    """Mark a queue row as ``rejected`` and record the decider.

    NO-AUTO-MERGE GUARD
    -------------------
    This function updates **only** the queue row (status, decided_by,
    decided_at).  It does NOT merge kg_nodes, does NOT write kg_edges,
    and does NOT modify any other table.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set.
    namespace_id:
        Active namespace UUID.  Explicit WHERE predicate in addition to RLS.
    queue_id:
        The ``id`` of the queue row to reject.
    decided_by:
        Identifier of the human (or authorised service) recording the
        decision.  Must be non-empty.

    Raises
    ------
    LookupError:
        The queue row does not exist in this namespace or is not in
        ``pending`` status.
    """
    await _set_decision(
        conn,
        namespace_id=namespace_id,
        queue_id=queue_id,
        new_status="rejected",
        decided_by=decided_by,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _to_uuid(value: str | UUID) -> UUID:
    """Coerce a str or UUID to UUID (matching project convention)."""
    return UUID(str(value)) if not isinstance(value, UUID) else value


async def _set_decision(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    queue_id: UUID,
    new_status: str,
    decided_by: str,
) -> None:
    """Internal: update status + decided_by + decided_at on a pending row.

    Only transitions from ``pending`` are permitted.  The WHERE predicate
    filters on both ``namespace_id`` (belt-and-braces on top of RLS) and
    ``status = 'pending'`` to prevent double-deciding a row.

    NO NODE MUTATIONS HERE — this touches only entity_merge_queue.
    """
    ns_uuid = _to_uuid(namespace_id)
    updated: str | None = await conn.fetchval(
        """
        UPDATE entity_merge_queue
        SET    status     = $1,
               decided_by = $2,
               decided_at = now()
        WHERE  id           = $3
          AND  namespace_id = $4
          AND  status       = 'pending'
        RETURNING id
        """,
        new_status,
        decided_by,
        queue_id,
        ns_uuid,
    )
    if updated is None:
        raise LookupError(
            f"Queue row {queue_id} not found in namespace {ns_uuid} "
            f"with status 'pending' — cannot set {new_status!r}."
        )


# ---------------------------------------------------------------------------
# Type alias re-export (for callers that need the datetime type hint)
# ---------------------------------------------------------------------------
__all__ = ["enqueue", "list_pending", "confirm", "reject"]
