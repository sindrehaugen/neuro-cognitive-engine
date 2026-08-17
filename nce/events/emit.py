"""nce.events.emit — graph-write event helper (C4 §9.6).

Responsibilities (one module, two cooperating functions):
- ``emit_graph_write`` builds the ``(node_type, op, id, namespace)`` event and
  delegates persistence to ``publish`` from ``nce.events.bus``.
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
