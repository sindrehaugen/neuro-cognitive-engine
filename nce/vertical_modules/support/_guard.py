"""
nce/vertical_modules/support/_guard.py
======================================
Shared namespace opt-in guard for the Support vertical.

Convention
----------
Matches the ``metadata->'<name>'->>'enabled'`` pattern used by the inventory,
product, economy, and d365 verticals.

The guard reads ``namespaces.metadata->'support'->>'enabled'`` for the supplied
``namespace_id`` and raises ``SupportDisabledError`` when the namespace has not opted in.

Apply this guard at the MCP handler (``handle_*``) and REST route (``api_*``)
boundary -- NOT inside the ``do_*`` cores. One call, one check (DRY).
"""

from __future__ import annotations

import logging
from typing import Any

from asyncpg.exceptions import DataError

log = logging.getLogger("nce.vertical_modules.support._guard")


class SupportDisabledError(Exception):
    """Raised when a namespace has not opted in to the Support vertical."""


async def require_support_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that ``metadata.support.enabled`` is ``true`` for *namespace_id*.

    Acquires a short-lived unmanaged connection from *pool* and queries the
    ``namespaces`` table. Raises :exc:`SupportDisabledError` when the
    namespace is unknown or has not set ``metadata->'support'->>'enabled'``
    to ``true``.

    Applied at the MCP handler / REST route boundary only -- never inside a
    ``do_*`` core, which stays ignorant of opt-in, of HTTP and of MCP.

    Parameters
    ----------
    pool:
        An ``asyncpg.Pool`` (or compatible mock with ``.acquire()`` as an
        async context manager).
    namespace_id:
        The tenant namespace UUID string to check.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(
                           (metadata->'support'->>'enabled')::boolean,
                           false
                       ) AS support_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except DataError as exc:
        log.info(
            "Support enabled-check got a malformed namespace_id=%r: %s",
            namespace_id,
            exc,
        )
        raise SupportDisabledError(
            f"Invalid namespace_id for Support vertical check: {namespace_id!r}"
        ) from exc

    if row is None or not row["support_enabled"]:
        log.info(
            "Support vertical not enabled for namespace_id=%s",
            namespace_id,
        )
        raise SupportDisabledError(
            f"Support vertical is not enabled for namespace {namespace_id}. "
            "Set metadata.support.enabled=true to opt in."
        )
