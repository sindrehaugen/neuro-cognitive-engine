"""nce.vertical_modules.notifications.service — Core service for notifications and subscriptions.

Phase A Wave A-3:
Provides transactional insertion and querying of notifications and subscriptions
with strict tenant isolation and idempotency guarantees.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]

log = logging.getLogger("nce.notifications.service")


async def create_notification(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID | str,
    principal_id: str,
    title: str,
    body: str = "",
    severity: str = "info",
    category: str = "general",
    source_selector: str | None = None,
    source_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Insert a notification idempotently into the notifications table.

    If a notification with the same (namespace_id, idempotency_key) already exists,
    the insert is safely skipped (ON CONFLICT DO NOTHING) and the existing row
    or an acknowledgment dictionary is returned.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    notif_id = uuid4()

    row = await conn.fetchrow(
        """
        INSERT INTO notifications (
            id, namespace_id, principal_id, title, body, severity,
            category, source_selector, source_id, idempotency_key
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (namespace_id, idempotency_key) WHERE idempotency_key IS NOT NULL
        DO NOTHING
        RETURNING *
        """,
        notif_id,
        ns_uuid,
        principal_id,
        title,
        body,
        severity,
        category,
        source_selector,
        source_id,
        idempotency_key,
    )

    if row is not None:
        return dict(row)

    # Conflict occurred — fetch existing row
    existing = await conn.fetchrow(
        """
        SELECT * FROM notifications
        WHERE namespace_id = $1 AND idempotency_key = $2
        """,
        ns_uuid,
        idempotency_key,
    )
    if existing is not None:
        result = dict(existing)
        result["idempotent_skip"] = True
        return result

    return {
        "ok": True,
        "idempotent_skip": True,
        "namespace_id": str(ns_uuid),
        "principal_id": principal_id,
        "idempotency_key": idempotency_key,
    }


async def subscribe_principal(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID | str,
    principal_id: str,
    selector: str,
) -> dict[str, Any]:
    """Register a principal's subscription to an event selector."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    row = await conn.fetchrow(
        """
        INSERT INTO notification_subscriptions (
            id, namespace_id, principal_id, selector
        )
        VALUES (gen_random_uuid(), $1, $2, $3)
        ON CONFLICT (namespace_id, principal_id, selector) DO NOTHING
        RETURNING *
        """,
        ns_uuid,
        principal_id,
        selector,
    )
    if row is not None:
        return dict(row)

    existing = await conn.fetchrow(
        """
        SELECT * FROM notification_subscriptions
        WHERE namespace_id = $1 AND principal_id = $2 AND selector = $3
        """,
        ns_uuid,
        principal_id,
        selector,
    )
    return dict(existing) if existing else {"ok": True, "subscribed": True}


async def get_subscribed_principals(
    conn: asyncpg.Connection,
    *,
    namespace_id: UUID | str,
    selector: str,
) -> list[str]:
    """Retrieve all principal_ids subscribed to a specific selector or wildcard '*' in the tenant."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    rows = await conn.fetch(
        """
        SELECT DISTINCT principal_id
        FROM notification_subscriptions
        WHERE namespace_id = $1 AND (selector = $2 OR selector = '*')
        """,
        ns_uuid,
        selector,
    )
    return [r["principal_id"] for r in rows]
