"""
nce/vertical_modules/field_tech/_guard.py
=========================================
Shared namespace opt-in guard for the Field Tech vertical.

Convention
----------
Matches the ``metadata->'field_tech'->>'enabled'`` pattern used by the support,
inventory, product, economy, and d365 verticals.

The guard reads ``namespaces.metadata->'field_tech'->>'enabled'`` for the supplied
``namespace_id`` and raises ``FieldTechDisabledError`` when the namespace has not opted in.

Apply this guard at the MCP handler (``handle_*``) and REST route (``api_*``)
boundary -- NOT inside the ``do_*`` cores. One call, one check (DRY).
"""

from __future__ import annotations

import logging
from typing import Any

from asyncpg.exceptions import DataError

log = logging.getLogger("nce.vertical_modules.field_tech._guard")


class FieldTechDisabledError(Exception):
    """Raised when a namespace has not opted in to the Field Tech vertical."""


async def require_field_tech_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that ``metadata.field_tech.enabled`` is ``true`` for *namespace_id*.

    Acquires a short-lived unmanaged connection from *pool* and queries the
    ``namespaces`` table. Raises :exc:`FieldTechDisabledError` when the
    namespace is unknown or has not set ``metadata->'field_tech'->>'enabled'``
    to ``true``.

    Applied at the MCP handler / REST route boundary only -- never inside a
    ``do_*`` core, which stays ignorant of opt-in, of HTTP and of MCP.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(
                           (metadata->'field_tech'->>'enabled')::boolean,
                           false
                       ) AS field_tech_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except DataError as exc:
        log.info(
            "Field Tech enabled-check got a malformed namespace_id=%r: %s",
            namespace_id,
            exc,
        )
        raise FieldTechDisabledError(
            f"Invalid namespace_id for Field Tech vertical check: {namespace_id!r}"
        ) from exc
    except Exception as exc:
        log.warning(
            "Field Tech enabled-check unexpected database error for namespace_id=%r: %s",
            namespace_id,
            exc,
        )
        raise

    if row is None:
        raise FieldTechDisabledError(f"Namespace {namespace_id!r} not found")

    if not row["field_tech_enabled"]:
        raise FieldTechDisabledError(
            f"Field Tech vertical is not enabled for namespace {namespace_id!r}"
        )
