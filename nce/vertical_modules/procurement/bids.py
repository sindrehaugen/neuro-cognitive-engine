"""
nce/vertical_modules/procurement/bids.py
=========================================
Procurement BID-price resolution — reads Product's price projections from the
``procurement_bid_prices`` consumer cache and returns the best BID per article.

Architecture (§9.1 / round-2 #5)
----------------------------------
Product (Module 2) owns the single Nettailer feed ingest and SKU identity.
Procurement NEVER reads the raw Nettailer feed or Product's ``product_prices``
table directly.

Data flow:
  1. Product pushes BID/supplier-price projections to this module via A2A/REST.
  2. ``upsert_bid_projection()`` writes rows into ``procurement_bid_prices``
     (ON CONFLICT DO UPDATE — consumer cache, no independent staleness clock).
  3. ``do_resolve_bids()`` reads the cache and returns the best BID per artnr.

Public surface
--------------
``upsert_bid_projection(conn, namespace_id, rows)``
    Upsert a batch of projection rows from Product into the consumer cache.
    Called by the A2A ingestion path (tests seed the cache directly via this).

``do_resolve_bids(engine, params) -> dict``
    ``{artnrs: [...]}`` → ``{results: [{artnr, leverandor, bid_id, pris}, ...]}``
    Returns the single best (lowest pris) BID per artnr from the cache.
    Capped at ``_MAX_ARTNRS`` articles per call.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.procurement.bids")

# Maximum number of artnr values accepted per resolve call (§spec cap).
_MAX_ARTNRS: int = 500


# ---------------------------------------------------------------------------
# Cache upsert — ingests Product's projection rows
# ---------------------------------------------------------------------------


async def upsert_bid_projection(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
    rows: list[dict[str, Any]],
) -> int:
    """Upsert a batch of BID projection rows into the consumer cache.

    Parameters
    ----------
    conn:
        An asyncpg connection with the tenant RLS context already set
        (i.e. ``set_namespace_context`` has been called or the caller uses
        ``scoped_pg_session``).
    namespace_id:
        The owning tenant namespace UUID.
    rows:
        List of projection dicts, each containing at minimum:
        ``artnr``, ``leverandor``, ``bid_id``.  Optional keys:
        ``prodid``, ``pris``, ``valid_to``.  The full dict is stored
        in the ``raw`` JSONB column for auditability.

    Returns
    -------
    int
        Number of rows upserted (inserted or updated).
    """
    if not rows:
        return 0

    synced_at = datetime.now(tz=timezone.utc)
    count = 0

    for row in rows:
        artnr: str = row["artnr"]
        leverandor: str = row["leverandor"]
        bid_id: str = row["bid_id"]
        prodid: str | None = row.get("prodid")
        pris: float | None = row.get("pris")
        valid_to_raw = row.get("valid_to")
        valid_to: datetime | None = None
        if isinstance(valid_to_raw, datetime):
            valid_to = valid_to_raw
        elif isinstance(valid_to_raw, str):
            try:
                valid_to = datetime.fromisoformat(valid_to_raw.replace("Z", "+00:00"))
            except ValueError:
                valid_to = None

        import json

        raw_json = json.dumps(row, default=str)

        await conn.execute(
            """
            INSERT INTO procurement_bid_prices
                (namespace_id, artnr, leverandor, bid_id, prodid, pris, valid_to, raw, synced_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            ON CONFLICT (namespace_id, artnr, leverandor, bid_id)
            DO UPDATE SET
                prodid    = EXCLUDED.prodid,
                pris      = EXCLUDED.pris,
                valid_to  = EXCLUDED.valid_to,
                raw       = EXCLUDED.raw,
                synced_at = EXCLUDED.synced_at
            """,
            namespace_id,
            artnr,
            leverandor,
            bid_id,
            prodid,
            pris,
            valid_to,
            raw_json,
            synced_at,
        )
        count += 1

    log.info(
        "[procurement-bids] upserted %d projection row(s) into procurement_bid_prices namespace=%s",
        count,
        namespace_id,
    )
    return count


# ---------------------------------------------------------------------------
# Best-BID resolver — reads the consumer cache
# ---------------------------------------------------------------------------


async def _fetch_best_bids(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
    artnrs: list[str],
) -> list[dict[str, Any]]:
    """Return the lowest-pris BID row per artnr from the cache.

    Uses a window function to pick the single best (MIN pris) row per artnr.
    Rows without a pris value are ranked last.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (artnr) artnr, leverandor, bid_id, pris, prodid
        FROM   procurement_bid_prices
        WHERE  namespace_id = $1
          AND  artnr        = ANY($2::text[])
        ORDER  BY artnr, pris ASC NULLS LAST
        """,
        namespace_id,
        artnrs,
    )
    return [dict(r) for r in rows]


async def do_resolve_bids(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the best BID price per article from the procurement_bid_prices cache.

    Parameters
    ----------
    engine:
        The live NCEEngine instance (provides ``pg_pool`` and namespace context).
    params:
        Must contain ``namespace_id`` (str UUID) and ``artnrs`` (list[str]).
        ``artnrs`` is capped at ``_MAX_ARTNRS`` (500) entries.

    Returns
    -------
    dict
        ``{"results": [{"artnr": ..., "leverandor": ..., "bid_id": ...,
                         "pris": ..., "prodid": ...}, ...]}``.
        Articles with no cached BID row are omitted from results.

    Raises
    ------
    ValueError
        If ``namespace_id`` or ``artnrs`` is missing or invalid.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("do_resolve_bids: 'namespace_id' is required")

    try:
        namespace_id = uuid.UUID(str(raw_ns))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"do_resolve_bids: invalid namespace_id {raw_ns!r}") from exc

    artnrs_raw: list[str] = params.get("artnrs") or []
    if not isinstance(artnrs_raw, list):
        raise ValueError("do_resolve_bids: 'artnrs' must be a list")

    artnrs = [str(a) for a in artnrs_raw[:_MAX_ARTNRS]]

    if not artnrs:
        return {"results": []}

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        results = await _fetch_best_bids(conn, namespace_id, artnrs)

    log.info(
        "[procurement-bids] do_resolve_bids: %d artnr(s) queried, %d resolved namespace=%s",
        len(artnrs),
        len(results),
        namespace_id,
    )
    return {"results": results}
