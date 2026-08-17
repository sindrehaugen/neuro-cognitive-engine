"""
nce/vertical_modules/product/_guard.py
=======================================
Shared namespace opt-in guard for the Product vertical.

Convention
----------
Matches the ``metadata->'<name>'->>'enabled'`` pattern used by the d365 and
consolidation verticals (see ``nce/cron.py`` and ``nce/admin_handlers/d365.py``).

The guard reads ``namespaces.metadata->'product'->>'enabled'`` for the
supplied ``namespace_id`` and raises ``ProductDisabledError`` when the
namespace has not opted in.

Apply this guard at the MCP handler (``handle_*``) and REST route (``api_*``)
boundary — NOT inside the ``do_*`` cores.  One call, one check (DRY).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asyncpg.exceptions import DataError

if TYPE_CHECKING:
    pass

log = logging.getLogger("nce.vertical_modules.product._guard")


class ProductDisabledError(Exception):
    """Raised when a namespace has not opted in to the Product vertical."""


async def require_product_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that ``metadata.product.enabled`` is ``true`` for *namespace_id*.

    Acquires a short-lived unmanaged connection from *pool* and queries the
    ``namespaces`` table.  Raises :exc:`ProductDisabledError` when the
    namespace is unknown or has not set ``metadata->'product'->>'enabled'``
    to ``true``.

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
                           (metadata->'product'->>'enabled')::boolean,
                           false
                       ) AS product_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except DataError as exc:
        # Defence in depth, not the primary control: REST callers
        # (nce/admin_handlers/product.py) validate namespace_id as a UUID
        # before this guard ever runs, so the ``::uuid`` cast above should
        # never see a malformed string. If a future caller (MCP/A2A) skips
        # that boundary check, asyncpg raises asyncpg.exceptions.DataError —
        # NOT a Python ValueError — which would otherwise escape this
        # function uncaught. Fail closed with the same structured refusal
        # used for a genuinely disabled namespace instead of letting the
        # driver exception propagate.
        log.info(
            "Product enabled-check got a malformed namespace_id=%r: %s",
            namespace_id,
            exc,
        )
        raise ProductDisabledError(
            f"Invalid namespace_id for Product vertical check: {namespace_id!r}"
        ) from exc

    if row is None or not row["product_enabled"]:
        log.info(
            "Product vertical not enabled for namespace_id=%s",
            namespace_id,
        )
        raise ProductDisabledError(
            f"Product vertical is not enabled for namespace {namespace_id}. "
            "Set metadata.product.enabled=true to opt in."
        )
