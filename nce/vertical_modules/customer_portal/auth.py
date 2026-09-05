"""
nce/vertical_modules/customer_portal/auth.py
============================================
Customer Principal Authentication & Isolation Discipline (Charter Layer 1).

Guarantees:
  1. Deny-when-unset: Unset or nil-UUID session scope exposes zero rows.
  2. IDOR refused: Session scope must exactly match record scope.
  3. Sets nce.external_scope_id GUC on pooled connection within transaction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg

from nce.db_utils import scoped_pg_session, set_external_scope

log = logging.getLogger("nce.vertical_modules.customer_portal.auth")

NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")


def evaluate_customer_scope_access(
    session_scope_id: UUID | str | None,
    record_scope_id: UUID | str,
) -> bool:
    """Evaluate whether session_scope_id is authorized to access record_scope_id.

    Replicates the database external_isolation_policy logic:
      - Deny-when-unset: Unset, empty, or nil-UUID session returns False.
      - Record with nil-UUID scope is never accessible.
      - Exact match required; cross-customer IDOR attempts are refused.
    """
    if not session_scope_id:
        return False

    try:
        session_uuid = (
            session_scope_id if isinstance(session_scope_id, UUID) else UUID(str(session_scope_id))
        )
    except (ValueError, TypeError):
        return False

    if session_uuid == NIL_UUID:
        return False

    try:
        record_uuid = (
            record_scope_id if isinstance(record_scope_id, UUID) else UUID(str(record_scope_id))
        )
    except (ValueError, TypeError):
        return False

    if record_uuid == NIL_UUID:
        return False

    return session_uuid == record_uuid


@asynccontextmanager
async def scoped_customer_pg_session(
    pool: asyncpg.Pool,
    namespace_id: str | UUID,
    customer_scope_id: str | UUID,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a connection scoped to BOTH tenant namespace and customer principal.

    Sets both 'nce.namespace_id' and 'nce.external_scope_id' transaction-locally.
    Clears automatically when the transaction completes.
    """
    scope_uuid = (
        customer_scope_id if isinstance(customer_scope_id, UUID) else UUID(str(customer_scope_id))
    )
    if scope_uuid == NIL_UUID:
        raise ValueError("customer_scope_id cannot be the nil-UUID deny sentinel")

    async with scoped_pg_session(pool, namespace_id) as conn:
        await set_external_scope(conn, scope_uuid)
        yield conn
