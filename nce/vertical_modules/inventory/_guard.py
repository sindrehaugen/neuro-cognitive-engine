"""
nce/vertical_modules/inventory/_guard.py
=========================================
Shared namespace opt-in guard for the Inventory vertical.

Convention
----------
Matches the ``metadata->'<name>'->>'enabled'`` pattern used by the d365 and
consolidation verticals (see ``nce/cron.py`` and ``nce/admin_handlers/d365.py``),
and mirrors ``nce/vertical_modules/economy/_guard.py`` and
``nce/vertical_modules/product/_guard.py`` exactly.

The guard reads ``namespaces.metadata->'inventory'->>'enabled'`` for the
supplied ``namespace_id`` and raises ``InventoryDisabledError`` when the
namespace has not opted in.

Apply this guard at the MCP handler (``handle_*``) and REST route (``api_*``)
boundary -- NOT inside the ``do_*`` cores.  One call, one check (DRY).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asyncpg.exceptions import DataError

if TYPE_CHECKING:
    pass

log = logging.getLogger("nce.vertical_modules.inventory._guard")


class InventoryDisabledError(Exception):
    """Raised when a namespace has not opted in to the Inventory vertical."""


async def require_inventory_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that ``metadata.inventory.enabled`` is ``true`` for *namespace_id*.

    Acquires a short-lived unmanaged connection from *pool* and queries the
    ``namespaces`` table.  Raises :exc:`InventoryDisabledError` when the
    namespace is unknown or has not set ``metadata->'inventory'->>'enabled'``
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
                           (metadata->'inventory'->>'enabled')::boolean,
                           false
                       ) AS inventory_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except DataError as exc:
        # Defence in depth, not the primary control: REST callers
        # (nce/admin_handlers/inventory.py) validate namespace_id as a UUID
        # before this guard ever runs, so the ``::uuid`` cast above should
        # never see a malformed string. If a future caller (MCP/A2A) skips
        # that boundary check, asyncpg raises asyncpg.exceptions.DataError --
        # NOT a Python ValueError -- which would otherwise escape this
        # function uncaught. Fail closed with the same structured refusal
        # used for a genuinely disabled namespace instead of letting the
        # driver exception propagate.
        log.info(
            "Inventory enabled-check got a malformed namespace_id=%r: %s",
            namespace_id,
            exc,
        )
        raise InventoryDisabledError(
            f"Invalid namespace_id for Inventory vertical check: {namespace_id!r}"
        ) from exc

    if row is None or not row["inventory_enabled"]:
        log.info(
            "Inventory vertical not enabled for namespace_id=%s",
            namespace_id,
        )
        raise InventoryDisabledError(
            f"Inventory vertical is not enabled for namespace {namespace_id}. "
            "Set metadata.inventory.enabled=true to opt in."
        )
