"""nce.vertical_modules.notifications.reminders — User reminder engine.

Phase A Wave A-3:
Manages user-set reminder deadlines attached to knowledge graph nodes,
periodically evaluated via cron to transition status and emit REMINDER.fired.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]

from nce.events.bus import publish

log = logging.getLogger("nce.notifications.reminders")


async def create_reminder(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID | str,
    principal_id: str,
    node_type: str,
    node_id: str,
    title: str,
    remind_at: datetime,
    note: str = "",
) -> dict[str, Any]:
    """Create a new pending reminder on a node in the estate."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    rem_id = uuid4()
    row = await conn.fetchrow(
        """
        INSERT INTO reminders (
            id, namespace_id, principal_id, node_type, node_id,
            title, note, remind_at, status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
        RETURNING *
        """,
        rem_id,
        ns_uuid,
        principal_id,
        node_type,
        node_id,
        title,
        note,
        remind_at,
    )
    return dict(row) if row else {}


async def fire_pending_reminders(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID | str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Scan and fire all pending reminders whose deadline has elapsed.

    Transitions status to 'fired', updates fired_at, and publishes REMINDER.fired
    to the C4 transactional outbox. Uses FOR UPDATE SKIP LOCKED to guarantee
    safe concurrent evaluation across scheduler instances.
    """
    effective_now = now or datetime.now(timezone.utc)

    if namespace_id is not None:
        ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
        rows = await conn.fetch(
            """
            SELECT id, namespace_id, principal_id, node_type, node_id,
                   title, note, remind_at, status, fired_at, is_archived,
                   created_at, updated_at
            FROM reminders
            WHERE namespace_id = $1
              AND status = 'pending'
              AND is_archived = FALSE
              AND remind_at <= $2
            ORDER BY remind_at ASC
            FOR UPDATE SKIP LOCKED
            """,
            ns_uuid,
            effective_now,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, namespace_id, principal_id, node_type, node_id,
                   title, note, remind_at, status, fired_at, is_archived,
                   created_at, updated_at
            FROM reminders
            WHERE status = 'pending'
              AND is_archived = FALSE
              AND remind_at <= $1
            ORDER BY remind_at ASC
            FOR UPDATE SKIP LOCKED
            """,
            effective_now,
        )

    fired_records: list[dict[str, Any]] = []
    for r in rows:
        rem_id = r["id"]
        r_ns = r["namespace_id"]
        await conn.execute(
            """
            UPDATE reminders
            SET status = 'fired',
                fired_at = $1,
                updated_at = $1
            WHERE id = $2 AND namespace_id = $3
            """,
            effective_now,
            rem_id,
            r_ns,
        )

        payload = {
            "reminder_id": str(rem_id),
            "namespace_id": str(r_ns),
            "principal_id": r["principal_id"],
            "node_type": r["node_type"],
            "node_id": r["node_id"],
            "title": r["title"],
            "note": r["note"] or "",
            "remind_at": r["remind_at"].isoformat() if r["remind_at"] else None,
            "fired_at": effective_now.isoformat(),
        }

        await publish(
            conn,
            namespace_id=r_ns,
            node_type="REMINDER",
            op="fired",
            aggregate_id=f"REMINDER:{rem_id}",
            payload=payload,
        )

        record = dict(r)
        record["status"] = "fired"
        record["fired_at"] = effective_now
        record["updated_at"] = effective_now
        fired_records.append(record)

    return fired_records
