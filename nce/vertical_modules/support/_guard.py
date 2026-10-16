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

import inspect
import logging
from typing import Any

from asyncpg.exceptions import DataError

from nce.engine_registry import EngineDisabledError

log = logging.getLogger("nce.vertical_modules.support._guard")


class SupportDisabledError(EngineDisabledError):
    """Raised when a namespace has not opted in to the Support vertical."""


_SUPPORT_ENABLED_QUERY = """
    SELECT COALESCE(
               (metadata->'support'->>'enabled')::boolean,
               false
           ) AS support_enabled
    FROM   namespaces
    WHERE  id = $1::uuid
"""


async def require_support_enabled(
    pool: Any,
    namespace_id: str | Any,
) -> None:
    """Assert that ``metadata.support.enabled`` is ``true`` for *namespace_id*.

    Accepts either an ``asyncpg.Pool`` (acquiring a short-lived connection) or
    an already-acquired ``asyncpg.Connection`` (calling ``fetchrow`` directly).
    Raises :exc:`SupportDisabledError` when the namespace is unknown or has not
    set ``metadata->'support'->>'enabled'`` to ``true``.

    Parameters
    ----------
    pool:
        An ``asyncpg.Pool`` or ``asyncpg.Connection`` (or compatible mock).
    namespace_id:
        The tenant namespace UUID (or string) to check.
    """
    try:
        if hasattr(pool, "acquire"):
            ctx = pool.acquire()
            if hasattr(ctx, "__aenter__"):
                async with ctx as conn:
                    row = await conn.fetchrow(_SUPPORT_ENABLED_QUERY, namespace_id)
            else:
                if inspect.iscoroutine(ctx):
                    ctx.close()
                row = await pool.fetchrow(_SUPPORT_ENABLED_QUERY, namespace_id)
        else:
            row = await pool.fetchrow(_SUPPORT_ENABLED_QUERY, namespace_id)
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
