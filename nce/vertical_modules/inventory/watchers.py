"""
nce/vertical_modules/inventory/watchers.py
============================================
Stock Watcher for the Inventory vertical module (Module 11, Wave 6b —
``restock-watcher``, B134b — split from B134's ``restock-advisor`` per
ML.md's §7.1 sizing audit: "A request-response advisor and a scheduled
Watcher are two sentences AND two runtimes.").

Role: **Watcher** — observes stock levels and the movement ledger, and
returns flags. **Return-only.** It writes nothing: no ``kg_nodes``, no
``kg_edges``, no ``inventory_items``, no ``event_log``.
``do_recommend_restock`` (B134) and ``do_create_restock_po`` (B137,
autonomy-governed under ``AUTONOMY_RESTOCK_CEILING``) are this vertical's
only writers; this module never calls either of them.

Two legs only — NOT three
--------------------------
The engine doc (``docs/vertical_engines/11-inventory-engine.md:82``) and
ML.md's B134b row both name a third leg: *expiring* stock (warranty/shelf-
bound SKUs). **The expiring leg is not built here and cannot be built on
this schema.** Verified on migration 050 (``inventory_items``: id,
namespace_id, sku, location_id, qty_on_hand, qty_reserved, qty_blocked,
reorder_point, created_at, updated_at) and migration 051
(``inventory_transactions``) — neither table carries an expiry, shelf-life,
lot, batch, or serial column. The engine doc itself files "lot/batch+recall
Watcher" under Tier B as a future direction, not shipped capability.
This is not a temporary oversight to paper over: there is no
expiry/lot/batch representation anywhere in the schema to key a leg from,
and ``inventory_items.created_at`` is the ROW's insert time (when this
namespace's stock record was created), not the physical product's
manufacture/receipt date — approximating from it would silently alert on
correctly-aged stock forever, which is worse than not alerting at all.
**What would unblock this leg:** a lot/batch/expiry representation added to
``inventory_items`` (or a new table), which needs its own migration number
and its own wave — not a config key, not a heuristic on an existing column.

Dependency direction (uncle-bob-craft)
-----------------------------------------
Imports only ``nce.db_utils.scoped_pg_session`` and ``nce.config`` — no
``nce.cron``, no web/admin adapter imports. The cron tick
(``nce/cron.py::_inventory_stock_watcher_tick``) imports this module's
:func:`do_flag_stock_alerts`; never the reverse.

Threshold source (settled, not guessed)
-----------------------------------------
Low stock compares ``available`` against ``inventory_items.reorder_point`` —
the column that already exists on the row. This module does **not** read
B134's ``nce/config_data/inventory-reorder-points.json``; that file is the
Advisor's input, not this Watcher's. If a landed Advisor treats the JSON as
authoritative *over* the column for the same ``(sku, location)``, that is a
real divergence between Advisor and Watcher this wave does not resolve —
see the wave brief's own note on this ambiguity.

Dead-stock zero-ledger-rows semantics (decided here, tested — ROUND 2 revision)
-----------------------------------------------------------------------------------
An ``inventory_items`` row with **zero** ``inventory_transactions`` rows is
dead stock **only once the row itself (``inventory_items.created_at``) is
older than** ``NCE_INVENTORY_DEAD_STOCK_DAYS`` — a never-moved item that has
not existed long enough to have HAD the chance to move is not yet stale
(it has not stopped moving; it has not started), but once it has existed
past the same window with zero history, that is exactly as damning as an
old last-movement date and exactly the pre-B139-migration case this
carve-out exists to still catch: rows that predate the ledger (B139 landed
mid-module) look identical to a genuinely never-moved item, and both are
capital sitting idle once enough time has passed to say so with any
confidence. Each such flag's ``rationale.window_ledger_ids`` is an explicit
empty list, so an auditor can see — by the field being present and empty,
not absent — that "no ledger rows exist" (not "rows exist outside the
window") is the verdict basis; ``rationale`` also carries the row's own
``created_at`` so the auditor can see which side of the cutoff put it there.
**Round 1 shipped this leg without the ``created_at`` carve-out** — every
zero-row item was flagged unconditionally, which double-flagged a brand-new
row (e.g. one also below its reorder point) as both low-stock AND dead on
the same tick it was created; that contradiction between this docstring and
``tests/test_inventory_stock_watcher.py::test_available_is_three_term_identity``
is what this revision resolves.

Quantity arithmetic
-----------------------------------------
All quantities are coerced via ``Decimal(str(x))``, never ``float`` —
``qty_on_hand`` etc. are ``NUMERIC(18,3)`` and four separate prior waves
(B120, B125, B127, B130) shipped a precision bug at exactly this boundary.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from nce.config import cfg
from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.inventory.watchers")

# Cap how many concrete `inventory_transactions.id` values are embedded per
# flag's rationale — alert-storm control, mirrors cron.py's
# _AGREEMENTS_ALERT_MAX_DETAILS (cron.py:714, "alert-storm control").
_RATIONALE_MAX_LEDGER_IDS: int = 5


def _as_decimal(x: Any) -> Decimal:
    """Coerce to Decimal via str() — never via float() (precision boundary)."""
    return Decimal(str(x))


async def _fetch_candidate_items(conn: Any, namespace_id: UUID) -> list[Any]:
    """Return every ``inventory_items`` row for *namespace_id* (explicit filter).

    Runs inside the caller's already-open ``scoped_pg_session`` — RLS is
    defense in depth here, not the only guard (rule 7: never rely on RLS
    alone).
    """
    return await conn.fetch(
        """
        SELECT id, sku, location_id, qty_on_hand, qty_reserved, qty_blocked,
               reorder_point, created_at
        FROM inventory_items
        WHERE namespace_id = $1::uuid
        """,
        str(namespace_id),
    )


def _check_low_stock(row: Any, as_of: datetime) -> dict[str, Any] | None:
    """Return a ``low_stock`` flag dict if ``available < reorder_point``, else None.

    ``available`` is the full three-term identity
    (``qty_on_hand - qty_reserved - qty_blocked``) — B130's identity, never
    shortcut to ``qty_on_hand`` alone. The boundary is strict ``<``: a row
    exactly AT its reorder point is not flagged.
    """
    qty_on_hand = _as_decimal(row["qty_on_hand"])
    qty_reserved = _as_decimal(row["qty_reserved"])
    qty_blocked = _as_decimal(row["qty_blocked"])
    reorder_point = _as_decimal(row["reorder_point"])
    available = qty_on_hand - qty_reserved - qty_blocked

    if available >= reorder_point:
        return None

    return {
        "flag_type": "low_stock",
        "sku": row["sku"],
        "location_id": str(row["location_id"]),
        "qty_on_hand": str(qty_on_hand),
        "qty_reserved": str(qty_reserved),
        "qty_blocked": str(qty_blocked),
        "available": str(available),
        "threshold": str(reorder_point),
        "rationale": {
            "as_of": as_of.isoformat(),
            "threshold": str(reorder_point),
            "basis": ("available = qty_on_hand - qty_reserved - qty_blocked < reorder_point"),
        },
    }


async def _check_dead_stock(
    conn: Any,
    namespace_id: UUID,
    row: Any,
    as_of: datetime,
    dead_stock_days: int,
) -> dict[str, Any] | None:
    """Return a ``dead_stock`` flag dict if ``qty_on_hand > 0`` with no recent movement.

    See the module docstring for the zero-ledger-rows decision: no rows at
    all is dead only once the item's own ``created_at`` also predates the
    cutoff (the carve-out that stops a brand-new row being flagged dead on
    the same tick it is created), with an explicit empty
    ``window_ledger_ids`` when it is.
    """
    qty_on_hand = _as_decimal(row["qty_on_hand"])
    if qty_on_hand <= 0:
        return None

    cutoff = as_of - timedelta(days=dead_stock_days)

    ledger_rows = await conn.fetch(
        """
        SELECT id, created_at
        FROM inventory_transactions
        WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
        ORDER BY created_at DESC
        LIMIT $4
        """,
        str(namespace_id),
        row["sku"],
        row["location_id"],
        _RATIONALE_MAX_LEDGER_IDS,
    )

    if not ledger_rows:
        item_created_at = row["created_at"]
        if item_created_at >= cutoff:
            # Not old enough yet to call "never moved" damning — see the
            # module docstring's ROUND 2 carve-out.
            return None
        return {
            "flag_type": "dead_stock",
            "sku": row["sku"],
            "location_id": str(row["location_id"]),
            "qty_on_hand": str(qty_on_hand),
            "threshold": dead_stock_days,
            "rationale": {
                "as_of": as_of.isoformat(),
                "threshold_days": dead_stock_days,
                "window_ledger_ids": [],
                "window_ledger_ids_cap": _RATIONALE_MAX_LEDGER_IDS,
                "item_created_at": item_created_at.isoformat(),
                "basis": (
                    "zero inventory_transactions rows for this "
                    "(namespace, sku, location) and item created_at "
                    "predates the dead-stock cutoff — see module docstring"
                ),
            },
        }

    most_recent = ledger_rows[0]["created_at"]
    if most_recent >= cutoff:
        return None

    return {
        "flag_type": "dead_stock",
        "sku": row["sku"],
        "location_id": str(row["location_id"]),
        "qty_on_hand": str(qty_on_hand),
        "threshold": dead_stock_days,
        "rationale": {
            "as_of": as_of.isoformat(),
            "threshold_days": dead_stock_days,
            "window_ledger_ids": [str(r["id"]) for r in ledger_rows],
            "window_ledger_ids_cap": _RATIONALE_MAX_LEDGER_IDS,
            "basis": (
                f"most recent movement {most_recent.isoformat()} predates "
                f"cutoff {cutoff.isoformat()}"
            ),
        },
    }


async def do_flag_stock_alerts(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Scan one namespace's ``inventory_items`` for low-stock and dead-stock flags.

    Return-only — writes nothing. See the module docstring for why the
    "expiring" leg named in the engine doc is not built here.

    Parameters
    ----------
    engine:
        Object with a ``pg_pool`` attribute (``asyncpg.Pool``).
    params:
        Must contain ``namespace_id`` (str or UUID).

    Returns
    -------
    dict
        ``namespace_id``, ``scanned`` (rows examined), ``flags`` (list of
        flag dicts, each carrying ``flag_type``, ``sku``, ``location_id``,
        the quantities that produced the verdict, the threshold, and a
        ``rationale`` with concrete ``inventory_transactions.id`` values
        where applicable).
    """
    namespace_id: UUID = UUID(str(params["namespace_id"]))
    pool = engine.pg_pool
    as_of = datetime.now(timezone.utc)
    dead_stock_days = int(cfg.NCE_INVENTORY_DEAD_STOCK_DAYS)

    flags: list[dict[str, Any]] = []

    async with scoped_pg_session(pool, namespace_id) as conn:
        rows = await _fetch_candidate_items(conn, namespace_id)
        scanned = len(rows)
        for row in rows:
            low = _check_low_stock(row, as_of)
            if low is not None:
                flags.append(low)
            dead = await _check_dead_stock(conn, namespace_id, row, as_of, dead_stock_days)
            if dead is not None:
                flags.append(dead)

    log.info(
        "do_flag_stock_alerts: namespace=%s scanned=%s flags=%s",
        namespace_id,
        scanned,
        len(flags),
    )
    return {"namespace_id": str(namespace_id), "scanned": scanned, "flags": flags}
