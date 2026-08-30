"""
nce/vertical_modules/inventory/stock.py
==========================================
Stock-core concurrency (Module 11, Wave 2 — ``stock-core-concurrency``):
``do_stock_levels`` / ``do_transfer_stock`` / ``do_record_consumption`` over
migration 050's ``inventory_items`` row, plus the ``STOCK_LOCATION`` /
``INVENTORY_ITEM -[at]->`` graph mirror.

Per ``docs/vertical_engines/11-inventory-engine.md`` (Build phase B1) and
``00-ENGINES-ROADMAP.md`` §2.11/§9.1/§9.5.

The row is authoritative; the graph node is a projection
--------------------------------------------------------
Migration 050's own docstring states it plainly: "``available`` is computed
at read time by a later wave's ``do_stock_levels``, not stored here. This row
is the SOURCE OF TRUTH for stock-truth reads ... a future ``INVENTORY_ITEM``
graph node is only an eventually-consistent projection of it, never the other
way around." Every function in this module honours that split:

  * :func:`do_stock_levels` reads ``inventory_items`` directly — never the
    graph mirror.
  * :func:`do_transfer_stock` / :func:`do_record_consumption` write
    ``inventory_items`` FIRST (the atomic decrement/increment, inside the
    scoped transaction), and only THEN mirror the resulting state into
    ``kg_nodes``/``kg_edges`` — a write that fails leaves no stale mirror
    behind, because the whole transaction (row write + mirror) commits or
    rolls back together (``scoped_pg_session`` wraps the block in one
    transaction).

The concurrency contract: ``UPDATE ... WHERE qty_on_hand >= n``
-----------------------------------------------------------------
Every decrement is ONE statement: the guard (enough stock exists) and the
write (subtract it) happen atomically under the row's own lock — never a
Python-side "SELECT then check then UPDATE", which races under concurrent
callers and can oversell the last unit. :func:`_decrement_on_hand` is the
sole implementation of this guard; both public decrementing functions call
it rather than each writing their own ad-hoc UPDATE.

Under PostgreSQL's default READ COMMITTED isolation, two concurrent
transactions decrementing the SAME row serialise on the row lock: the
second transaction's ``UPDATE`` blocks until the first commits, then
re-evaluates ``qty_on_hand >= n`` against the just-committed value — so a
racing decrement that would drive the row negative instead affects **zero
rows**, and this module raises :class:`InsufficientStockError` rather than
assuming success. ``tests/test_inventory_stock.py`` proves this with REAL
concurrent connections (``asyncio.gather`` over separate pool connections),
not sequential calls on one connection, which would prove nothing about the
race.

Negative stock is structurally impossible via TWO independent mechanisms
(belt and braces, not either-or):
  1. This module's own ``WHERE qty_on_hand >= n`` guard refuses the write
     before it would go negative.
  2. Migration 050's ``CHECK (qty_on_hand >= 0)`` on the column itself would
     refuse it even if this module's guard were ever bypassed (a raw-SQL fix,
     a future bug) — the database, not just this code, is the last line of
     defence.

The reservation algebra
------------------------
``available = qty_on_hand - qty_reserved - qty_blocked`` (roadmap's A2,
adopted verbatim). This wave has no writer for ``qty_reserved`` /
``qty_blocked`` (``do_reserve_stock`` is a later wave's job per the engine
doc's Build phase B4) — every row this module creates has both at their
column default of ``0``, so ``available == qty_on_hand`` for all data this
wave writes. :func:`do_stock_levels` still always computes the full
three-term subtraction (not a shortcut that assumes zero) so it stays
correct the instant a later wave starts writing non-zero reservations.

Quantity precision — NUMERIC(18,3), Decimal end-to-end
----------------------------------------------------------
``inventory_items.qty_on_hand`` / ``qty_reserved`` / ``qty_blocked`` are all
``NUMERIC(18,3)`` (migration 050 — some SKUs are stocked by fractional unit,
e.g. cable by the metre). :func:`_as_quantity` coerces every caller-supplied
quantity to an exact ``Decimal`` and quantises it to the column's own 3dp
scale in Python (:func:`_quantise_qty`) BEFORE it is bound to any query —
the code decides the rounding, not Postgres silently truncating an
unquantised value on the way in (the same defect class a prior wave shipped
against a ``NUMERIC(5,4)`` column). ``bool`` is rejected before the ``int``
branch (``isinstance(True, int)`` is ``True`` in Python); non-finite floats
(NaN/Infinity) and non-numeric types are rejected; a quantity must quantise
to strictly ``> 0`` — a zero or negative "move this much stock" request is
refused, not silently treated as a no-op.

Graph mirror: kg-upsert template + a known, flagged gap
----------------------------------------------------------
The node/edge upsert SQL follows the ``ON CONFLICT ... DO UPDATE`` shape of
the kg-upsert template this wave's brief names explicitly
(``nce/vertical_modules/dynamics365/sync.py``'s ``_upsert_kg_node`` /
``_upsert_kg_edges_batch``), including the transactional-outbox
``emit_graph_write`` call after every node upsert (edges never call it —
``kg_edges`` has no FK to ``kg_nodes``, so an edge write is always a safe,
un-gated write; mirrors ``economy/graph.py``'s ``_upsert_edge`` /
``procurement/graph.py``'s ``upsert_offers_edge`` reasoning).

**Ownership gap CLOSED (Batch 130a); one gap remains, disclosed below.**
``STOCK_LOCATION``, ``INVENTORY_ITEM``, ``GOODS_RECEIPT`` and
``INVENTORY_RMA`` are now registered in ``nce/config_data/
node-ownership.json`` with ``owner_engine: "inventory"``, and every NODE
upsert in this module is gated behind ``nce.entity_resolution.ownership.
assert_owner`` (Contract A, deny-by-default against
``node_ownership_registry``) — matching ``economy/graph.py``,
``product/graph.py`` and ``system_design/graph.py``'s guarded template
rather than ``dynamics365/sync.py``'s unguarded one (that D365 writer stays
unguarded on purpose: it is an external-system sync with authority-
precedence on conflict, not this module's concern).

**Operator-facing consequence, stated explicitly:** ``node_ownership_registry``
is a PER-NAMESPACE table, and seeding it is additive-only
(``seed_node_ownership_registry``, insert-only). A namespace whose registry
has **not yet** been seeded with these four rows will have its entire
``do_transfer_stock`` / ``do_record_consumption`` transaction ABORTED the
moment either function tries to mirror a write — the authoritative
``inventory_items`` row write AND the ``inventory_transactions`` ledger
row(s) ``append_transaction`` already wrote (Module 11, Wave 11 — B139; it
runs BEFORE the mirror at every call site in this module) both included,
because the ledger append, the row write, and the mirror all run inside the
SAME transaction.

This is not a caller-free vacuum: Batch 131 already put both functions on
production-reachable surfaces — ``nce/admin_handlers/inventory.py``'s
stock-levels/transfer/consumption handlers, wired to routes in
``nce/admin_app.py`` and registered as MCP tools in ``nce/tool_registry.py``.
An unseeded namespace's transfer/consumption request through EITHER the
admin/REST route or the MCP tool call fails closed the same way — this
guard is not landing ahead of its callers. The namespace self-heals the next
time the orchestrator boots (``nce/orchestrator.py:454-481`` backfills every
existing namespace immediately after migrations run), but until then every
transfer/consumption in that namespace fails closed, through every surface
that reaches it.

``change_origin='agent'`` (not ``'sync'``, which is reserved for the D365
external-sync origin) is unchanged — these remain engine-authored writes.
No ``inventory_source_id`` column exists yet (kg_nodes/kg_edges only carry
``d365_source_id`` / ``procurement_source_id`` / ``economy_source_id``
today) — this half of the original gap is still deliberately deferred (see
``OUT OF SCOPE`` in the B130a wave brief); only the *ownership* half was
discharged by this change.

A van/warehouse's own ``kind``/``name`` metadata is not owned by this module
(``schema_seed.py`` owns ``stock_locations`` rows) — the ``STOCK_LOCATION``
mirror node this module writes is keyed purely by ``location_id`` (the UUID
this module already has from its own params), never by name.

Dependency direction (uncle-bob-craft)
-----------------------------------------
This module imports only ``asyncpg``, ``nce.db_utils.scoped_pg_session``,
``nce.entity_resolution.ownership.assert_owner`` (peer domain core, same
direction as ``economy/graph.py``), ``nce.events.emit.emit_graph_write``, and
(Module 11, Wave 11 — ``transactions-valuation``)
``nce.vertical_modules.inventory.transactions``'s typed reason constants +
``append_transaction`` — no web/HTTP/admin framework imports, matching
``schema_seed.py`` and ``economy/contracts.py``'s convention. ``NCEEngine``
is imported under ``TYPE_CHECKING`` only.

Wave 11 addition: the immutable transaction ledger
------------------------------------------------------
:func:`do_transfer_stock` and :func:`do_record_consumption` each call
``transactions.append_transaction`` on the SAME ``conn`` already inside their
``scoped_pg_session`` block — never a separate connection or transaction — so
a movement's row write and its ledger row commit or roll back together by
construction. This does not reopen Batch 130's deadlock: an
``inventory_transactions`` INSERT takes no lock any concurrent transaction
could contend with resource-for-resource. It never upserts (no
``ON CONFLICT``, always a fresh ``gen_random_uuid()`` row, so two concurrent
inserts never want the SAME row), and the only lock it acquires on shared
state is a ``FOR KEY SHARE`` on the referenced ``stock_locations`` row (its
FK) — the identical lock KIND ``_increment_on_hand``'s own INSERT already
takes on that same row, and ``FOR KEY SHARE`` is mutually compatible with
itself (any number of transactions may hold it concurrently; it only
conflicts with an UPDATE of the row's key columns or a DELETE, neither of
which happens anywhere in this module). Two locks that never conflict with
each other cannot form a wait-for cycle, in any relative order — so the
ledger append is placed immediately after each row write it documents
(inside the existing ``if decrement_first: ... else: ...`` branches for
:func:`do_transfer_stock`) purely for readability, not because the ordering
is load-bearing for deadlock-avoidance the way the row/mirror lock order is.
``tests/test_inventory_stock.py``'s
``test_cross_sku_opposite_direction_transfers_do_not_deadlock`` and
``test_same_sku_opposite_direction_transfers_do_not_deadlock`` were re-run
against this change and still hold both cases closed live.

Cross-location lock ordering: canonical, direction-independent
-----------------------------------------------------------------
:func:`do_transfer_stock` touches AT LEAST FOUR lockable resources per
transfer — the two ``inventory_items`` rows and the two ``STOCK_LOCATION``
mirror nodes (``_upsert_kg_node``'s ``ON CONFLICT (label, namespace_id) DO
UPDATE`` takes a row lock on ``kg_nodes`` exactly as the item ``UPDATE``
does) — and in practice about eight, since the mirror also locks the two
``INVENTORY_ITEM`` nodes and two ``kg_edges`` rows, and the increment's
``INSERT`` takes ``FOR KEY SHARE`` FK locks on ``stock_locations``. Those
additional resources are SKU-scoped and are reached THROUGH
:func:`_mirror_item_at_location`, which is invoked in canonical order and is
internally deterministic, so they inherit the same ordering. It acquires
ALL of them in ONE order that is a pure function of the RESOURCE PAIR —
``sorted((from_location, to_location))`` by UUID, see
:func:`_canonical_lock_order` — never of the transfer's direction. Two
concurrent transfers between the same pair therefore queue behind one
another instead of forming a wait cycle, whichever way each is moving stock.

Ordering the row writes ALONE is not a fix, and this is the trap worth
spelling out: the ``kg_nodes`` half of the cycle is **SKU-independent**, so
if the mirror stays direction-ordered then ANY two-way traffic between the
same two locations deadlocks — including transfers of two DIFFERENT SKUs,
which share no ``inventory_items`` row at all. Two independent audits
measured exactly that against this module. Rates, not raw counts, because the
two runs used different loop lengths — and note the unit: each ROUND issues
two transfer SIDES, and a deadlocked round aborts exactly one of them. With
nothing ordered, cross-SKU opposite-direction transfers deadlocked in
**100% of rounds** (60 aborted sides across 120 sides; independently
re-measured at 24 of 24 rounds). With only the two ``inventory_items`` writes
ordered, the cycle simply relocated onto ``kg_nodes`` and still deadlocked in
**100% of rounds** (12 aborted sides across 24 sides; re-measured 24 of 24).
With BOTH the rows and the mirror following this one canonical order,
**zero** deadlocks — 0 aborted sides across 120 cross-SKU and 24 same-SKU
sides, and 0 of 60 rounds on the re-measurement.
``tests/test_inventory_stock.py``'s
``test_cross_sku_opposite_direction_transfers_do_not_deadlock`` and
``test_same_sku_opposite_direction_transfers_do_not_deadlock`` hold both
cases closed live.

Consequence, stated explicitly because it looks wrong at a glance: when
``to_location`` sorts BEFORE ``from_location``, the increment runs before the
decrement. That is safe and intended — both writes are in the same
transaction, the ``from_location == to_location`` guard still rejects a
self-transfer, the ``WHERE qty_on_hand >= n`` guard still refuses an
oversell, and any refusal rolls the whole transfer back (the already-applied
increment included), so no intermediate state is ever observable outside the
transaction.

One behaviour DID change with the reorder, and it is named here rather than
left to be rediscovered: when a transfer is BOTH short of stock at
``from_location`` AND names a ``to_location`` that is not a real
``stock_locations`` row, the exception type is now order-dependent —
``from < to`` raises :class:`InsufficientStockError` (the decrement runs
first), ``to < from`` raises :class:`ValueError` (the FK violation runs
first). Before the reorder it was always :class:`InsufficientStockError`.
Both are documented raise types of this function and both roll the whole
transfer back, so there is no correctness or money impact — but a caller
branching on the exception type for a doubly-invalid request must expect
either.

:func:`do_record_consumption` touches ONE location, and follows the same
rows-before-mirror sequence, so it cannot form a cycle against a transfer
either: every writer in this module takes ``inventory_items`` locks before
``kg_nodes`` locks, and takes per-location locks in ascending-UUID order.

What is NOT closed: a caller retry loop. This module orders its own locks; it
cannot order them against a FUTURE module that writes the same
``STOCK_LOCATION``/``INVENTORY_ITEM`` mirror nodes in a different sequence
relative to ``inventory_items`` — that would reintroduce a cycle this module
alone cannot prevent. PostgreSQL would abort one side with
``deadlock_detected`` rather than corrupt data (conservation of total
quantity holds regardless — pinned every round by the tests named above), but
no retry-on-deadlock wrapper is implemented here.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write
from nce.vertical_modules.inventory.transactions import (
    REASON_CONSUMPTION,
    REASON_TRANSFER_IN,
    REASON_TRANSFER_OUT,
    append_transaction,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.stock")

# ---------------------------------------------------------------------------
# Graph mirror constants
# ---------------------------------------------------------------------------

_NODE_TYPE_STOCK_LOCATION = "STOCK_LOCATION"
_NODE_TYPE_INVENTORY_ITEM = "INVENTORY_ITEM"
_PRED_AT = "at"
# Must equal node-ownership.json's "owner_engine" for STOCK_LOCATION /
# INVENTORY_ITEM exactly — _internal/tools/coverage_check.py:754 already
# declares Obligation("STOCK_LOCATION", None, ("inventory",)) against it.
_OWNER_ENGINE = "inventory"
# Engine-authored write, not an external-system sync — see module docstring's
# "Graph mirror" section for why this differs from D365 sync's 'sync' origin.
_CHANGE_ORIGIN = "agent"
# The mirror is written synchronously, in the same transaction as the row
# write it reflects — so at the instant it is written it is maximally
# fresh. Roadmap's "confidence = stock-count freshness" note anticipates a
# later wave introducing decay; this wave has no staleness source yet.
_FRESH_CONFIDENCE = 1.0

# ---------------------------------------------------------------------------
# Quantity coercion — Decimal end-to-end, quantised to inventory_items'
# own NUMERIC(18,3) column scale (see module docstring).
# ---------------------------------------------------------------------------

_QTY_SCALE: Decimal = Decimal("0.001")
_ZERO_QTY: Decimal = Decimal("0.000")


def _quantise_qty(value: Decimal, where: str) -> Decimal:
    """Round *value* to inventory_items' own column scale (3dp), ties away
    from zero. Mirrors economy/contracts.py's ``_quantise`` discipline."""
    try:
        return value.quantize(_QTY_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: quantity is too large to express to 3dp: {value!r}") from exc


def _as_quantity(value: Any, where: str) -> Decimal:
    """Coerce a required, strictly-positive quantity to an exact ``Decimal``,
    quantised to 3dp. Rejects ``None``, ``bool`` (checked before ``int`` —
    ``isinstance(True, int)`` is ``True`` in Python), non-finite floats, any
    non-numeric type, and anything that quantises to ``<= 0``."""
    if value is None:
        raise ValueError(f"{where}: a quantity is required, got None")
    if isinstance(value, bool):
        raise ValueError(f"{where}: bool is not a quantity, got {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        try:
            # Decimal(str(x)), never Decimal(x) — the latter imports the
            # binary-float representation error.
            candidate = Decimal(str(value))
        except DecimalException as exc:  # pragma: no cover - str(float) always parses
            raise ValueError(f"{where}: not a usable number: {value!r}") from exc
    else:
        raise ValueError(
            f"{where}: expected int/float/Decimal, got {type(value).__name__} {value!r}"
        )
    if not candidate.is_finite():  # NaN, sNaN, +-Infinity
        raise ValueError(f"{where}: must be finite, got {value!r}")
    candidate = _quantise_qty(candidate, where)
    if candidate <= _ZERO_QTY:
        raise ValueError(f"{where}: must be > 0, got {candidate}")
    return candidate


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _as_sku(raw: Any, where: str) -> str:
    sku = str(raw or "").strip()
    if not sku:
        raise ValueError(f"{where}: 'sku' is required")
    return sku


def _as_location_uuid(raw: Any, where: str) -> UUID:
    if not raw:
        raise ValueError(f"{where}: a location id is required")
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise ValueError(f"{where}: expected a UUID string, got {raw!r}") from exc


def _canonical_lock_order(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Return the two locations in ascending-UUID order.

    THE single lock-acquisition order for every resource a transfer touches
    — both ``inventory_items`` rows and both mirror nodes. Deliberately a
    pure function of the resource pair, so A→B and B→A produce the IDENTICAL
    order and cannot form a wait cycle (module docstring, "Cross-location
    lock ordering").
    """
    return (a, b) if a < b else (b, a)


# ---------------------------------------------------------------------------
# Domain error — distinct from a plain input-validation ValueError: this one
# is a business-rule refusal (not enough stock), carrying enough structure
# for a caller to report WHY without re-parsing a message string.
# ---------------------------------------------------------------------------


class InsufficientStockError(Exception):
    """Raised when a decrement's ``WHERE qty_on_hand >= n`` guard affects
    zero rows — either the row exists but does not hold enough stock, or no
    ``inventory_items`` row exists yet for this ``(sku, location)`` (treated
    as ``available_on_hand=0``, which is the correct current state)."""

    def __init__(
        self,
        *,
        sku: str,
        location_id: UUID,
        requested: Decimal,
        available_on_hand: Decimal,
    ) -> None:
        self.sku = sku
        self.location_id = location_id
        self.requested = requested
        self.available_on_hand = available_on_hand
        super().__init__(
            f"insufficient stock for sku={sku!r} at location={location_id}: "
            f"requested {requested}, only {available_on_hand} on hand"
        )


# ---------------------------------------------------------------------------
# DB-touching: the atomic decrement/increment primitives.
# Both public write functions below call these — neither writes its own
# ad-hoc UPDATE, so the row-lock guard has exactly one implementation.
# ---------------------------------------------------------------------------


async def _decrement_on_hand(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    sku: str,
    location_id: UUID,
    qty: Decimal,
) -> Decimal:
    """Atomically decrement ``qty_on_hand`` by *qty* at one (sku, location).

    ONE statement performs both the guard and the write
    (``UPDATE ... WHERE qty_on_hand >= n``) — never a Python-side
    check-then-act. Returns the POST-decrement ``qty_on_hand``.

    Raises
    ------
    InsufficientStockError
        The guard affected zero rows (not enough stock, or no row at all).
    """
    row = await conn.fetchrow(
        """
        UPDATE inventory_items
        SET qty_on_hand = qty_on_hand - $4, updated_at = now()
        WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
          AND qty_on_hand >= $4
        RETURNING qty_on_hand
        """,
        str(ns_uuid),
        sku,
        str(location_id),
        qty,
    )
    if row is not None:
        return row["qty_on_hand"]  # type: ignore[no-any-return]

    # Guard failed. A diagnostic-only read (no decision hinges on it — the
    # refusal above is already final) to report the actual current qty, or 0
    # when no row exists at all for this (sku, location).
    current = await conn.fetchval(
        """
        SELECT qty_on_hand FROM inventory_items
        WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
        """,
        str(ns_uuid),
        sku,
        str(location_id),
    )
    raise InsufficientStockError(
        sku=sku,
        location_id=location_id,
        requested=qty,
        available_on_hand=current if current is not None else _ZERO_QTY,
    )


async def _increment_on_hand(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    sku: str,
    location_id: UUID,
    qty: Decimal,
) -> Decimal:
    """Atomically increment ``qty_on_hand`` by *qty* at one (sku, location),
    creating the row (at ``qty_on_hand = qty``) if it does not exist yet.

    ONE upsert statement — no read-then-write. Returns the
    POST-increment ``qty_on_hand``.

    Raises
    ------
    ValueError
        *location_id* is not a ``stock_locations`` row in this namespace
        (the composite FK on ``inventory_items.location_id`` refuses it) —
        translated from the raw ``ForeignKeyViolationError`` into a clearer
        domain message.
    """
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO inventory_items (namespace_id, sku, location_id, qty_on_hand)
            VALUES ($1::uuid, $2, $3::uuid, $4)
            ON CONFLICT (namespace_id, sku, location_id) DO UPDATE
                SET qty_on_hand = inventory_items.qty_on_hand + EXCLUDED.qty_on_hand,
                    updated_at  = now()
            RETURNING qty_on_hand
            """,
            str(ns_uuid),
            sku,
            str(location_id),
            qty,
        )
    except asyncpg.ForeignKeyViolationError as exc:
        raise ValueError(
            f"do_transfer_stock: to_location {location_id} does not exist in this namespace"
        ) from exc

    assert row is not None  # RETURNING on INSERT ... DO UPDATE always yields a row
    return row["qty_on_hand"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Graph mirror — kg_nodes/kg_edges upsert. See module docstring's "Graph
# mirror" section: node upserts are assert_owner-guarded (Contract A); the
# remaining, deliberately deferred gap is inventory_source_id, not ownership.
# ---------------------------------------------------------------------------


def _stock_location_label(location_id: UUID) -> str:
    return f"StockLocation:{location_id}"


def _inventory_item_label(sku: str, location_id: UUID) -> str:
    return f"InventoryItem:{sku}:{location_id}"


async def _upsert_kg_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
    entity_type: str,
) -> None:
    """Upsert a single kg_nodes row and emit the transactional outbox event.

    Guarded by ``assert_owner`` (deny-by-default when no registry row
    exists — Contract A), checked FIRST so a refusal writes nothing at all.

    No ``inventory_source_id`` column exists yet, so no per-engine source tag
    is written (see module docstring's flagged gap).
    """
    await assert_owner(conn, ns_uuid, entity_type, _OWNER_ENGINE)

    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3::uuid, $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type   = EXCLUDED.entity_type,
                change_origin = EXCLUDED.change_origin,
                updated_at    = NOW()
        """,
        label,
        entity_type,
        str(ns_uuid),
        _CHANGE_ORIGIN,
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=entity_type,
        op="upserted",
        node_id=label,
    )


async def _upsert_kg_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    subject_label: str,
    predicate: str,
    object_label: str,
    confidence: float,
) -> None:
    """Upsert a single kg_edges row. ``confidence`` lives on the edge only
    (rule 7) — never on either node. No ownership check: edges have no FK to
    kg_nodes, so this write is always safe regardless of which engine owns
    either endpoint's node type (mirrors economy/graph.py's ``_upsert_edge``
    / procurement/graph.py's ``upsert_offers_edge`` reasoning)."""
    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
        VALUES ($1, $2, $3, $4, $5::uuid, $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence    = EXCLUDED.confidence,
                change_origin = EXCLUDED.change_origin,
                updated_at    = NOW()
        """,
        subject_label,
        predicate,
        object_label,
        confidence,
        str(ns_uuid),
        _CHANGE_ORIGIN,
    )


async def _mirror_item_at_location(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    sku: str,
    location_id: UUID,
) -> None:
    """Mirror ``INVENTORY_ITEM -[at]-> STOCK_LOCATION`` for one (sku,
    location) pair — an eventually-consistent PROJECTION of the row this
    module just wrote, never itself read back as stock truth."""
    location_label = _stock_location_label(location_id)
    item_label = _inventory_item_label(sku, location_id)
    await _upsert_kg_node(conn, ns_uuid, location_label, _NODE_TYPE_STOCK_LOCATION)
    await _upsert_kg_node(conn, ns_uuid, item_label, _NODE_TYPE_INVENTORY_ITEM)
    await _upsert_kg_edge(conn, ns_uuid, item_label, _PRED_AT, location_label, _FRESH_CONFIDENCE)


# ---------------------------------------------------------------------------
# Public: do_stock_levels — the "own stock first" read. Always the ROW,
# never the graph mirror.
# ---------------------------------------------------------------------------


async def do_stock_levels(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Live per-SKU-per-location stock, read from the authoritative
    ``inventory_items`` row — never the graph mirror.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "sku":          str,          # optional filter
            "location":     str | UUID,   # optional filter, a stock_locations id
        }``

    Returns
    -------
    dict
        ``{"ok": True, "items": [{"sku", "location_id", "on_hand",
        "reserved", "blocked", "available"}, ...]}``, one entry per matching
        ``inventory_items`` row. ``available`` is always
        ``on_hand - reserved - blocked`` (the full three-term identity, not
        a shortcut — see module docstring's "reservation algebra" section).
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")

    conditions = ["namespace_id = $1::uuid"]
    args: list[Any] = [str(ns_uuid)]

    raw_sku = params.get("sku")
    if raw_sku is not None:
        sku = _as_sku(raw_sku, "sku")
        args.append(sku)
        conditions.append(f"sku = ${len(args)}")

    raw_location = params.get("location")
    if raw_location is not None:
        location_uuid = _as_location_uuid(raw_location, "location")
        args.append(str(location_uuid))
        conditions.append(f"location_id = ${len(args)}::uuid")

    query = f"""
        SELECT sku, location_id, qty_on_hand, qty_reserved, qty_blocked,
               (qty_on_hand - qty_reserved - qty_blocked) AS available
        FROM inventory_items
        WHERE {" AND ".join(conditions)}
        ORDER BY sku, location_id
    """

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(query, *args)

    items = [
        {
            "sku": row["sku"],
            "location_id": str(row["location_id"]),
            "on_hand": row["qty_on_hand"],
            "reserved": row["qty_reserved"],
            "blocked": row["qty_blocked"],
            "available": row["available"],
        }
        for row in rows
    ]
    return {"ok": True, "items": items}


# ---------------------------------------------------------------------------
# Public: do_transfer_stock — the physical warehouse<->van / van<->van move.
# ---------------------------------------------------------------------------


async def do_transfer_stock(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Move *qty* units of *sku* from one stock location to another.

    Parameters
    ----------
    params:
        ``{
            "namespace_id":  str | UUID,  # required
            "sku":           str,          # required
            "qty":           int | float | Decimal,  # required, > 0
            "from_location": str | UUID,  # required, a stock_locations id
            "to_location":   str | UUID,  # required, a stock_locations id
        }``

    The decrement at ``from_location`` and the increment at ``to_location``
    happen in the SAME transaction (``scoped_pg_session``) as the graph
    mirror for both locations and the two ``inventory_transactions`` ledger
    rows this appends (Module 11, Wave 11 — a ``transfer_out`` row at
    ``from_location``, a ``transfer_in`` row at ``to_location``, each's
    ``ref`` the counterpart location's id; ``unit_cost=None`` — no cost
    source exists yet, see ``transactions.py``'s "Honest scope limit") —
    either the whole transfer (both row changes + both mirror updates + both
    ledger rows) commits, or none of it does.

    Both the row writes and the mirror writes are applied in
    :func:`_canonical_lock_order` (ascending UUID), NOT in transfer
    direction — so the increment sometimes precedes the decrement. See the
    module docstring's "Cross-location lock ordering" section for why that is
    both safe and necessary (ordering the rows alone leaves a SKU-independent
    deadlock cycle on the mirror nodes).

    Returns
    -------
    dict
        ``{"ok": True, "sku", "from_location", "to_location", "qty",
        "from_on_hand", "to_on_hand"}`` — the POST-transfer on-hand qty at
        each location.

    Raises
    ------
    ValueError
        Any required field missing/malformed, ``from_location ==
        to_location``, or ``to_location`` is not a valid location in this
        namespace.
    InsufficientStockError
        ``from_location`` does not hold at least *qty* units of *sku*.
    OwnershipError
        The namespace's ``node_ownership_registry`` has no (or a differently
        owned) row for ``STOCK_LOCATION``/``INVENTORY_ITEM`` when the graph
        mirror tries to write it. This ABORTS THE ENTIRE TRANSACTION,
        including the already-applied ``inventory_items`` row write AND the
        ``inventory_transactions`` ledger row(s) already appended by this
        call (they run BEFORE the mirror) — the refusal is not limited to
        the mirror (see module docstring's "Graph mirror" section).
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    sku = _as_sku(params.get("sku"), "sku")
    qty = _as_quantity(params.get("qty"), "qty")
    from_location = _as_location_uuid(params.get("from_location"), "from_location")
    to_location = _as_location_uuid(params.get("to_location"), "to_location")

    if from_location == to_location:
        raise ValueError("do_transfer_stock: 'from_location' and 'to_location' must differ")

    first, second = _canonical_lock_order(from_location, to_location)
    decrement_first = first == from_location

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        if decrement_first:
            from_on_hand = await _decrement_on_hand(conn, ns_uuid, sku, from_location, qty)
            await append_transaction(
                conn,
                ns_uuid,
                sku=sku,
                location_id=from_location,
                delta=-qty,
                reason_category=REASON_TRANSFER_OUT,
                ref=str(to_location),
            )
            to_on_hand = await _increment_on_hand(conn, ns_uuid, sku, to_location, qty)
            await append_transaction(
                conn,
                ns_uuid,
                sku=sku,
                location_id=to_location,
                delta=qty,
                reason_category=REASON_TRANSFER_IN,
                ref=str(from_location),
            )
        else:
            to_on_hand = await _increment_on_hand(conn, ns_uuid, sku, to_location, qty)
            await append_transaction(
                conn,
                ns_uuid,
                sku=sku,
                location_id=to_location,
                delta=qty,
                reason_category=REASON_TRANSFER_IN,
                ref=str(from_location),
            )
            from_on_hand = await _decrement_on_hand(conn, ns_uuid, sku, from_location, qty)
            await append_transaction(
                conn,
                ns_uuid,
                sku=sku,
                location_id=from_location,
                delta=-qty,
                reason_category=REASON_TRANSFER_OUT,
                ref=str(to_location),
            )

        await _mirror_item_at_location(conn, ns_uuid, sku, first)
        await _mirror_item_at_location(conn, ns_uuid, sku, second)

    log.info(
        "do_transfer_stock: ns=%s sku=%s qty=%s from=%s(%s) to=%s(%s)",
        ns_uuid,
        sku,
        qty,
        from_location,
        from_on_hand,
        to_location,
        to_on_hand,
    )
    return {
        "ok": True,
        "sku": sku,
        "from_location": str(from_location),
        "to_location": str(to_location),
        "qty": qty,
        "from_on_hand": from_on_hand,
        "to_on_hand": to_on_hand,
    }


# ---------------------------------------------------------------------------
# Public: do_record_consumption — a tech picks/uses stock for a job.
# ---------------------------------------------------------------------------


async def do_record_consumption(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Decrement stock when it is picked/used at one location (e.g. a
    Field Tech work-order); realises demand, no destination location.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "sku":          str,          # required
            "qty":          int | float | Decimal,  # required, > 0
            "location":     str | UUID,  # required, a stock_locations id
            "work_order":   str,          # optional, pass-through reference
        }``

        ``work_order`` is accepted, echoed back, AND (Module 11, Wave 11)
        persisted as the appended ``inventory_transactions`` row's ``ref`` —
        so it is retrievable from the ledger, not just this call's response.

    Returns
    -------
    dict
        ``{"ok": True, "sku", "location", "qty", "on_hand", "work_order"}``
        — ``on_hand`` is the POST-consumption ``qty_on_hand`` at *location*.

    Raises
    ------
    ValueError
        Any required field missing/malformed, or ``work_order`` given but
        not a string.
    InsufficientStockError
        *location* does not hold at least *qty* units of *sku*.
    OwnershipError
        The namespace's ``node_ownership_registry`` has no (or a differently
        owned) row for ``STOCK_LOCATION``/``INVENTORY_ITEM`` when the graph
        mirror tries to write it. This ABORTS THE ENTIRE TRANSACTION,
        including the already-applied ``inventory_items`` row write AND the
        ``inventory_transactions`` ledger row(s) already appended by this
        call (they run BEFORE the mirror) — the refusal is not limited to
        the mirror (see module docstring's "Graph mirror" section).
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    sku = _as_sku(params.get("sku"), "sku")
    qty = _as_quantity(params.get("qty"), "qty")
    location = _as_location_uuid(params.get("location"), "location")

    work_order = params.get("work_order")
    if work_order is not None and not isinstance(work_order, str):
        raise ValueError(
            f"do_record_consumption: 'work_order' must be a string when given, "
            f"got {type(work_order).__name__}"
        )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        on_hand = await _decrement_on_hand(conn, ns_uuid, sku, location, qty)
        await append_transaction(
            conn,
            ns_uuid,
            sku=sku,
            location_id=location,
            delta=-qty,
            reason_category=REASON_CONSUMPTION,
            ref=work_order,
        )
        await _mirror_item_at_location(conn, ns_uuid, sku, location)

    log.info(
        "do_record_consumption: ns=%s sku=%s qty=%s location=%s on_hand=%s work_order=%s",
        ns_uuid,
        sku,
        qty,
        location,
        on_hand,
        work_order,
    )
    return {
        "ok": True,
        "sku": sku,
        "location": str(location),
        "qty": qty,
        "on_hand": on_hand,
        "work_order": work_order,
    }
