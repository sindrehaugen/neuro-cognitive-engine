"""
nce/vertical_modules/product/_guard.py
=======================================
Shared namespace opt-in guards for the Product vertical.

Convention
----------
Matches the ``metadata->'<name>'->>'enabled'`` pattern used by the d365 and
consolidation verticals (see ``nce/cron.py`` and ``nce/admin_handlers/d365.py``).

``require_product_enabled`` reads ``namespaces.metadata->'product'->>'enabled'``
for the supplied ``namespace_id`` and raises ``ProductDisabledError`` when the
namespace has not opted in to the Product vertical as a whole.

``require_nettailer_source_enabled`` is a narrower, per-source sibling: it
gates only the Nettailer feed (``metadata->'product'->'sources'->>'nettailer'``),
not the whole vertical — a namespace can enable Product without enabling
every individual feed source.

Apply either guard at the MCP handler (``handle_*``) and REST route (``api_*``)
boundary — NOT inside the ``do_*`` cores.  One call, one check (DRY).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asyncpg.exceptions import DataError

from nce.engine_registry import EngineDisabledError

if TYPE_CHECKING:
    pass

log = logging.getLogger("nce.vertical_modules.product._guard")


class ProductDisabledError(EngineDisabledError):
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


class NettailerSourceDisabledError(EngineDisabledError):
    """Raised when a namespace has not opted in to the Nettailer product feed.

    Deliberately NOT a subclass of :exc:`ProductDisabledError`: this is a
    per-source opt-in, not "the Product vertical is disabled." A namespace
    can have Product enabled and Nettailer specifically not, or vice versa.
    Reusing ``require_product_enabled`` here would gate every other product
    source (e.g. the manufacturer API adapter) on the same flag, which
    nothing asked for and nothing in this codebase currently does.
    """


async def require_nettailer_source_enabled(
    pool: Any,
    namespace_id: str,
) -> None:
    """Assert that ``metadata.product.sources.nettailer`` is ``true`` for *namespace_id*.

    Same shape as :func:`require_product_enabled` (short-lived connection,
    ``COALESCE .. false``, fail-closed on a malformed ``namespace_id``), keyed
    one level deeper so it gates only the Nettailer feed, not the Product
    vertical as a whole. Raises :exc:`NettailerSourceDisabledError` when the
    namespace is unknown or has not opted this source in.

    Apply this at whatever boundary first calls
    ``nce.vertical_modules.product.sources.nettailer.stream_nettailer_rows``
    (MCP handler, REST route, or a scheduled sync job) — that function itself
    takes no ``namespace_id`` and does no DB access by design (parse+
    normalise+yield only), so it cannot check this on its own.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(
                           (metadata->'product'->'sources'->>'nettailer')::boolean,
                           false
                       ) AS nettailer_enabled
                FROM   namespaces
                WHERE  id = $1::uuid
                """,
                namespace_id,
            )
    except DataError as exc:
        log.info(
            "Nettailer source enabled-check got a malformed namespace_id=%r: %s",
            namespace_id,
            exc,
        )
        raise NettailerSourceDisabledError(
            f"Invalid namespace_id for Nettailer source check: {namespace_id!r}"
        ) from exc

    if row is None or not row["nettailer_enabled"]:
        log.info(
            "Nettailer source not enabled for namespace_id=%s",
            namespace_id,
        )
        raise NettailerSourceDisabledError(
            f"Nettailer product feed is not enabled for namespace {namespace_id}. "
            "Set metadata.product.sources.nettailer=true to opt in."
        )
