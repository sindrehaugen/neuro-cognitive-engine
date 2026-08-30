"""nce.events.emit — graph-write event helper (C4 §9.6).

Responsibilities (one module, cooperating functions):
- ``emit_graph_write`` builds the ``(node_type, op, id, namespace)`` event and
  delegates persistence to ``publish`` from ``nce.events.bus``.  This is the
  BASE payload contract — 22 ``op="upserted"`` call sites plus the
  ``memory.stored`` handler depend on exactly these four keys and nothing
  else.  It is byte-for-byte unchanged by this module (Wave M0.W20b).
- ``emit_status_change`` is an ADDITIVE sibling (Wave M0.W20b — c4-payload-
  contract): same base four keys, plus ``project_id`` and ``status`` for
  consumers that need to act on a status transition rather than merely
  observe that a node changed — e.g. Module 7's BOM_LINE-status handlers,
  which require both.  It does not touch ``emit_graph_write`` or any of its
  call sites; the two helpers share only the ``publish`` plumbing.  Wiring a
  real producer to call this is out of scope here (see W20c).
- ``mark_graph_write_processed`` records an event_id in ``processed_outbox_events``
  (INSERT … ON CONFLICT DO NOTHING) so at-least-once relay delivery is idempotent.

Design invariants (uncle-bob-craft):
- Each function does exactly one thing.
- Dependencies point inward: only ``asyncpg`` and ``nce.events.bus`` are imported.
- No web adapters, admin modules, or Django imports.
- Dedup reuses the existing ``processed_outbox_events`` table — no new mechanism.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.events.bus import publish


async def emit_graph_write(
    conn: asyncpg.Connection,
    *,
    namespace_id: Any,
    node_type: str,
    op: str,
    node_id: str,
) -> None:
    """Emit a graph-write event to the transactional outbox.

    Builds the ``(node_type, op, id, namespace)`` event payload and inserts it
    into ``outbox_events`` via ``publish``.  Must be called inside an open
    transaction so post-commit delivery semantics are preserved.

    Parameters
    ----------
    conn:         Open asyncpg connection (must be inside an active transaction).
    namespace_id: Tenant UUID.
    node_type:    Entity / aggregate type (e.g. ``"account"``, ``"contact"``).
    op:           Operation label (e.g. ``"upserted"``).
    node_id:      Domain identifier of the affected kg_node (label or UUID string).
    """
    payload: dict[str, Any] = {
        "node_type": node_type,
        "op": op,
        "id": node_id,
        "namespace": str(namespace_id),
    }
    await publish(
        conn,
        namespace_id=namespace_id,
        node_type=node_type,
        op=op,
        aggregate_id=node_id,
        payload=payload,
    )


async def emit_status_change(
    conn: asyncpg.Connection,
    *,
    namespace_id: Any,
    node_type: str,
    op: str,
    node_id: str,
    project_id: str,
    status: str,
) -> None:
    """Emit a status-change graph-write event carrying ``project_id``/``status``.

    Additive sibling of ``emit_graph_write`` (Wave M0.W20b): the payload is
    the SAME base four keys (``node_type``, ``op``, ``id``, ``namespace``)
    plus two more — ``project_id`` and ``status`` — for consumers that need
    to act on a status transition rather than merely observe that a node
    changed.

    This does not touch ``emit_graph_write`` or any of its 22+ existing call
    sites; it is a new, independent function reusing the same ``publish``
    plumbing.  Use this at a status-change write site (e.g. ``BOM_LINE``
    advancing to a new status) where a downstream handler — such as Module
    7's ``_handle_bom_line_status_changed`` / ``_handle_po_status_changed`` /
    ``_handle_goods_receipt_created`` — requires ``project_id`` and
    ``status`` to do its job.  Producer wiring itself (calling this from a
    real domain write site) is out of scope here; see W20c.

    Parameters
    ----------
    conn:         Open asyncpg connection (must be inside an active transaction).
    namespace_id: Tenant UUID.
    node_type:    Entity / aggregate type whose status changed (e.g. ``"BOM_LINE"``).
    op:           Operation label (e.g. ``"status_changed"``).
    node_id:      Domain identifier of the affected kg_node (label or UUID string).
    project_id:   Owning PROJECT label the status change is scoped to.
    status:       The new status value.
    """
    payload: dict[str, Any] = {
        "node_type": node_type,
        "op": op,
        "id": node_id,
        "namespace": str(namespace_id),
        "project_id": project_id,
        "status": status,
    }
    await publish(
        conn,
        namespace_id=namespace_id,
        node_type=node_type,
        op=op,
        aggregate_id=node_id,
        payload=payload,
    )


async def mark_graph_write_processed(
    conn: asyncpg.Connection,
    *,
    event_id: uuid.UUID,
    namespace_id: Any,
) -> bool:
    """Record *event_id* in ``processed_outbox_events`` for idempotent dedup.

    Uses INSERT … ON CONFLICT DO NOTHING so a duplicate delivery of the same
    ``event_id`` is silently skipped.

    Returns ``True`` when the event is newly recorded (first delivery),
    ``False`` when it was already present (duplicate — caller should skip
    re-processing).

    Parameters
    ----------
    conn:         Open asyncpg connection (any transaction state is fine; the
                  INSERT is autocommitted when no surrounding transaction exists,
                  or participates in the caller's transaction).
    event_id:     UUID from ``outbox_events.id`` — the relay-assigned identity.
    namespace_id: Tenant UUID — required FK on ``processed_outbox_events``.
    """
    status = await conn.execute(
        """
        INSERT INTO processed_outbox_events (event_id, namespace_id)
        VALUES ($1, $2::uuid)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id,
        str(namespace_id),
    )
    # asyncpg returns "INSERT 0 N" — N=1 means newly inserted, N=0 means conflict.
    return status.endswith("1")
