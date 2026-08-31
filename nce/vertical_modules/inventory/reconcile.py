"""
nce/vertical_modules/inventory/reconcile.py
=============================================
Dead-stock reconciliation between the authoritative ``inventory_items`` rows
(migration 050) and the append-only ``inventory_transactions`` ledger
(migration 051) — Module 11, Wave 10c (``dead-stock-reconcile``).

Dead stock is where a divergence can sit undetected longest, precisely
because nothing is moving to expose it: the Watcher concern in
``docs/vertical_engines/11-inventory-engine.md`` §Intelligence — *dead-stock
detection (no movement in N days — capital sitting idle)*. This module takes
every dead ``(sku, location)`` pair and asks one question of it: does the
authoritative quantity still agree with the ledger's own arithmetic?

It raises. It does not log-and-continue.
------------------------------------------
A reconcile that silently diverges from the ledger is the exact failure the
ledger exists to prevent. ``log.warning(...)`` followed by ``continue`` or
``return`` is the shape that left another module's automation dead on main
for weeks — the caller reads the return as success and the divergence is
lost permanently. So: :func:`do_reconcile_dead_stock` classifies the WHOLE
dead set first, and if the ``divergent`` bucket is non-empty it raises
:class:`LedgerDivergenceError` carrying **every** diverging pair as
structured data. There is no ``--force``, no "best effort", no partial
success, and no parameter that downgrades the raise.

It writes NOTHING AT ALL
--------------------------
No row, no ``kg_nodes`` mirror, no outbox event, not even a "reconciled_at"
marker — on the clean path and on the raising path alike. A reconcile that
repairs what it finds cannot also be the thing that tells you the truth
about it; repair is a separate concern and is not built here. The single
namespace-scoped read runs inside ``scoped_pg_session`` and every predicate
carries ``namespace_id = $1::uuid`` EXPLICITLY — RLS is defence in depth, not
the scoping mechanism (an owner pool bypasses FORCE RLS, and a same-label row
in another namespace would otherwise be readable).

Honest scope limit — the unknown opening balance
--------------------------------------------------
A pair whose ``inventory_items`` row predates its own ledger history has an
**unknown opening balance**. Nothing in this schema records what that pair
held before its first ledger row was appended: there is no opening-balance
column, no snapshot table, and this wave creates neither. For such a pair
``qty_on_hand`` and ``SUM(delta)`` are simply not comparable — they measure
different things — so comparing them would manufacture false divergences out
of missing history. Those pairs are therefore reported in their own bucket,
``unreconcilable_opening_balance``, counted, and **never asserted about in
either direction**: never called balanced, never called divergent. Reporting
them is the honest answer; excusing them into ``balanced`` would hide real
divergences, and condemning them into ``divergent`` would raise on data that
was never wrong.

Why ``>=`` is the ledger-born test
------------------------------------
Every writer creates the ``inventory_items`` row *before* appending its
ledger row, in the SAME transaction, and Postgres' ``now()`` is the
transaction timestamp — so a ledger-born pair has ``items.created_at ==
MIN(txn.created_at)`` exactly, its opening balance is provably ``0``, and
``qty_on_hand == SUM(delta)`` is an exact identity. A pair seeded before the
ledger existed (or seeded directly by a test or by ``schema_seed.py``) has
``items.created_at < MIN(txn.created_at)`` strictly. The ``>`` case — ledger
history predating the row itself — is anomalous and is deliberately
classified as ledger-born, so it surfaces as a divergence rather than being
quietly excused.

Dependency direction (uncle-bob-craft)
-----------------------------------------
This module imports only ``nce.db_utils.scoped_pg_session`` — no
asyncpg-specific API, no web/HTTP/admin framework imports and nothing from
another vertical module. ``NCEEngine`` is imported under ``TYPE_CHECKING`` only, matching
``transactions.py``'s and ``stock.py``'s convention. The classification is a
pure, DB-free function over plain rows (:func:`classify_dead_stock_pairs`),
the same split ``transactions.py`` makes with its valuation math — which is
what makes the loud-failure behaviour unit-testable with no Postgres at all.

Coercion helpers are duplicated, not imported
-----------------------------------------------
``stock.py``'s and ``rma.py``'s coercion helpers are module-private
(leading underscore) and this wave does not touch either file. This module
carries its own small copies, the same
duplication-over-cross-module-private-import choice ``transactions.py``
already makes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import UUID

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.reconcile")

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"
_DEAD_STOCK_CONFIG_FILENAME = "inventory-dead-stock.json"

BUCKET_BALANCED = "balanced"
BUCKET_UNRECONCILABLE = "unreconcilable_opening_balance"
BUCKET_DIVERGENT = "divergent"


def load_inventory_dead_stock_config() -> dict[str, Any]:
    """Load and return the contents of ``inventory-dead-stock.json``.

    Returns
    -------
    dict with key ``dead_stock_days`` (a non-negative int). Global — not
    namespace-scoped — for this wave (see the file's own ``_comment``).
    """
    path = _CONFIG_DATA_DIR / _DEAD_STOCK_CONFIG_FILENAME
    with path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    return config


# ---------------------------------------------------------------------------
# Input coercion — ``transactions.py::_as_decimal``'s idiom: bool is rejected
# BEFORE the int branch, because ``isinstance(True, int)`` is ``True``.
# ---------------------------------------------------------------------------


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _as_dead_stock_days(raw: Any, where: str) -> int:
    """Coerce a dead-stock window to a non-negative ``int``.

    ``bool`` is rejected before the ``int`` branch (``isinstance(True, int)``
    is ``True`` in Python); a string — even ``"90"`` — is rejected rather
    than parsed, so a mis-typed config or caller fails loudly instead of
    silently widening or narrowing the window.
    """
    if isinstance(raw, bool):
        raise ValueError(f"{where}: bool is not a number of days, got {raw!r}")
    if not isinstance(raw, int):
        raise ValueError(f"{where}: expected a non-negative int, got {type(raw).__name__} {raw!r}")
    if raw < 0:
        raise ValueError(f"{where}: must be >= 0, got {raw}")
    return raw


class LedgerDivergenceError(Exception):
    """``inventory_items`` no longer agrees with the ``inventory_transactions``
    ledger for one or more dead ``(sku, location)`` pairs.

    Carries the full list as STRUCTURED data on :attr:`pairs` — each entry a
    dict with ``sku``, ``location_id``, ``on_hand``, ``ledger_sum`` and
    ``difference`` — so a caller never has to re-parse a message string.
    ``__str__`` names EVERY diverging pair, not a sample and not a count:
    "how many, and which" is the first question anyone asks.
    """

    def __init__(self, pairs: Sequence[Mapping[str, Any]]) -> None:
        self.pairs: list[dict[str, Any]] = [dict(pair) for pair in pairs]
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [
            f"  sku={pair['sku']!r} location_id={pair['location_id']} "
            f"on_hand={pair['on_hand']} ledger_sum={pair['ledger_sum']} "
            f"difference={pair['difference']}"
            for pair in self.pairs
        ]
        head = (
            f"inventory_items diverges from the inventory_transactions ledger "
            f"for {len(self.pairs)} dead (sku, location) pair(s):"
        )
        return "\n".join([head, *lines])

    def __str__(self) -> str:
        return self._render()


# ---------------------------------------------------------------------------
# Pure classification — no DB, no asyncpg awareness. Takes plain rows and
# returns the three buckets, the same split transactions.py makes with its
# valuation math (uncle-bob-craft rule 10).
# ---------------------------------------------------------------------------


class DeadStockBuckets(NamedTuple):
    """The three reported buckets. Every dead pair lands in exactly one."""

    balanced: list[dict[str, Any]]
    unreconcilable_opening_balance: list[dict[str, Any]]
    divergent: list[dict[str, Any]]


def classify_dead_stock_pairs(rows: Sequence[Mapping[str, Any]]) -> DeadStockBuckets:
    """Classify already-fetched dead pairs into the three buckets.

    Each input row carries ``sku``, ``location_id``, ``on_hand``
    (``Decimal``), ``items_created_at`` (``datetime``), ``ledger_txn_count``
    (``int``), ``ledger_sum`` (``Decimal`` or ``None`` when there are no
    ledger rows) and ``ledger_first_at`` (``datetime`` or ``None``).

    Pure and DB-free on purpose: the loud-failure behaviour this module
    exists for is decided here, so it is testable with no Postgres at all.
    This function NEVER raises on a divergence — it reports every pair it was
    given; the raise is the caller's single, post-classification decision
    (:func:`do_reconcile_dead_stock`), so one divergence can never hide the
    ones after it.
    """
    balanced: list[dict[str, Any]] = []
    unreconcilable: list[dict[str, Any]] = []
    divergent: list[dict[str, Any]] = []

    for row in rows:
        sku = str(row["sku"])
        location_id = str(row["location_id"])
        on_hand: Decimal = row["on_hand"]
        ledger_first_at: datetime | None = row.get("ledger_first_at")
        ledger_txn_count = int(row.get("ledger_txn_count") or 0)
        raw_sum = row.get("ledger_sum")
        ledger_sum: Decimal | None = None if raw_sum is None else raw_sum
        items_created_at: datetime = row["items_created_at"]

        # No ledger history at all, or a row that existed before its own
        # ledger did: the opening balance is unknown and unrecorded, so this
        # pair is reported and never asserted about in either direction.
        if (
            ledger_txn_count == 0
            or ledger_first_at is None
            or ledger_sum is None
            or items_created_at < ledger_first_at
        ):
            unreconcilable.append(
                {
                    "sku": sku,
                    "location_id": location_id,
                    "on_hand": on_hand,
                    "ledger_sum": ledger_sum,
                    "ledger_txn_count": ledger_txn_count,
                    "reason": (
                        "no ledger history"
                        if ledger_txn_count == 0
                        else "inventory_items row predates its own ledger history"
                    ),
                }
            )
            continue

        # Ledger-born: opening balance is provably 0, so qty_on_hand ==
        # SUM(delta) is an exact identity.
        entry = {
            "sku": sku,
            "location_id": location_id,
            "on_hand": on_hand,
            "ledger_sum": ledger_sum,
        }
        if on_hand == ledger_sum:
            balanced.append(entry)
        else:
            divergent.append({**entry, "difference": on_hand - ledger_sum})

    return DeadStockBuckets(
        balanced=balanced,
        unreconcilable_opening_balance=unreconcilable,
        divergent=divergent,
    )


_DEAD_SET_SQL = """
    SELECT
        i.sku                              AS sku,
        i.location_id                      AS location_id,
        i.qty_on_hand                      AS on_hand,
        i.created_at                       AS items_created_at,
        COALESCE(t.txn_count, 0)           AS ledger_txn_count,
        t.ledger_sum                       AS ledger_sum,
        t.first_at                         AS ledger_first_at,
        t.last_at                          AS ledger_last_at
    FROM inventory_items i
    LEFT JOIN (
        SELECT
            sku,
            location_id,
            COUNT(*)          AS txn_count,
            SUM(delta)        AS ledger_sum,
            MIN(created_at)   AS first_at,
            MAX(created_at)   AS last_at
        FROM inventory_transactions
        WHERE namespace_id = $1::uuid
        GROUP BY sku, location_id
    ) t ON t.sku = i.sku AND t.location_id = i.location_id
    WHERE i.namespace_id = $1::uuid
      AND i.qty_on_hand > 0
      AND (
            t.last_at IS NULL
            OR t.last_at < now() - make_interval(days => $2::int)
          )
    ORDER BY i.sku ASC, i.location_id ASC
"""


async def do_reconcile_dead_stock(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Reconcile every dead ``(sku, location)`` pair against the ledger.

    A pair is *dead* when it holds stock (``qty_on_hand > 0``) and its latest
    ``inventory_transactions`` row is older than the configured window — pairs
    with NO ledger row at all are dead too, by the same reasoning (nothing has
    moved).

    Parameters
    ----------
    params:
        ``{
            "namespace_id":    str | UUID,  # required
            "dead_stock_days": int,          # optional override; default from
                                             # nce/config_data/inventory-dead-stock.json
        }``

    Returns
    -------
    dict
        ``{"ok": True, "dead_stock_days": N, "dead_pairs": <count>,
        "balanced": [...], "unreconcilable_opening_balance": [...],
        "divergent": []}`` — every bucket in FULL, never truncated, never
        sampled, never summarised to a count alone. A long list is
        information, not noise.

    Raises
    ------
    ValueError
        ``namespace_id`` missing/malformed, or ``dead_stock_days`` is a
        ``bool``, a non-int, or negative.
    LedgerDivergenceError
        One or more ledger-born dead pairs no longer agree with the ledger.
        The WHOLE dead set is classified first, so the error carries every
        diverging pair — never just the first one found.

    This function writes nothing at all: no row, no ``kg_nodes`` mirror, no
    outbox event, no marker — on the clean path and on the raising path
    alike.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")

    if "dead_stock_days" in params and params["dead_stock_days"] is not None:
        dead_stock_days = _as_dead_stock_days(
            params["dead_stock_days"], "do_reconcile_dead_stock: dead_stock_days"
        )
    else:
        config = load_inventory_dead_stock_config()
        dead_stock_days = _as_dead_stock_days(
            config.get("dead_stock_days"),
            "do_reconcile_dead_stock: inventory-dead-stock.json 'dead_stock_days'",
        )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        db_rows = await conn.fetch(_DEAD_SET_SQL, str(ns_uuid), dead_stock_days)

    # Detach from asyncpg.Record into plain dicts — the classification has no
    # DB awareness at all (uncle-bob-craft: dependencies point inward).
    rows: list[dict[str, Any]] = [
        {
            "sku": record["sku"],
            "location_id": record["location_id"],
            "on_hand": record["on_hand"],
            "items_created_at": record["items_created_at"],
            "ledger_txn_count": record["ledger_txn_count"],
            "ledger_sum": record["ledger_sum"],
            "ledger_first_at": record["ledger_first_at"],
        }
        for record in db_rows
    ]

    # Classify the WHOLE dead set, THEN decide. Raising inside the loop would
    # hide every divergence after the first.
    buckets = classify_dead_stock_pairs(rows)

    if buckets.divergent:
        raise LedgerDivergenceError(buckets.divergent)

    log.info(
        "do_reconcile_dead_stock: %d dead pair(s) over %d day(s) — %d balanced, "
        "%d with an unreconcilable opening balance, 0 divergent",
        len(rows),
        dead_stock_days,
        len(buckets.balanced),
        len(buckets.unreconcilable_opening_balance),
    )

    return {
        "ok": True,
        "dead_stock_days": dead_stock_days,
        "dead_pairs": len(rows),
        BUCKET_BALANCED: buckets.balanced,
        BUCKET_UNRECONCILABLE: buckets.unreconcilable_opening_balance,
        BUCKET_DIVERGENT: buckets.divergent,
    }
