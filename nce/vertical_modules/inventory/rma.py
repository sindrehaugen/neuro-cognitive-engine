"""
nce/vertical_modules/inventory/rma.py
========================================
Returns/RMA + WEEE disposal state (Module 11, Wave 10 — ``rma-table``),
migration 053's ``inventory_rma`` table.

Per ``docs/vertical_engines/11-inventory-engine.md`` (§B5 "Returns/RMA +
WEEE") and ``docs/vertical_engines/11a-inventory-engine-research.md`` finding
130: Rackbeat has no dedicated RMA object at all — ``INVENTORY_RMA`` plus a
first-class WEEE (Waste Electrical and Electronic Equipment) compliance state
is a genuine addition over off-the-shelf WMS, not a copy of one.

This module records; it does not move — read this section before anything else
--------------------------------------------------------------------------------
:func:`do_record_rma` writes exactly one ``inventory_rma`` row and mirrors
exactly one ``INVENTORY_RMA`` node into the graph. It writes **no**
``inventory_transactions`` row, calls **no** function in ``stock.py``, and
leaves ``inventory_items`` untouched. ``stock_movement_state`` is written as
``'pending'`` and nothing else — this module implements no transition of it.
Both stock legs (restock-on-return, permanent WEEE disposal) belong to Batch
138b, which is exactly why ``location_id`` / ``qty`` / ``stock_movement_state``
are provisioned on this table already: Batch 138b needs no DDL of its own.
Anything that looks like it wants to touch ``inventory_transactions`` or
``inventory_items`` from this module is a bug in this module, not a feature.

INSERT-only, never UPDATE — the seam Batch 138b relies on
-------------------------------------------------------------
The one write statement is ``INSERT ... ON CONFLICT (namespace_id, rma_ref)
DO NOTHING RETURNING id``. When the natural key already exists, this module
reads the EXISTING row back and returns it with ``created=False`` — it does
not modify a single column. There is no update path in this module, by
construction. This is deliberate: Batch 138b writes ``stock_movement_state``
and ``weee_state`` as it performs the two stock legs, and an upsert here
(``DO UPDATE`` instead of ``DO NOTHING``) could reset an already-settled RMA
back to ``'pending'`` and cause Batch 138b to double-restock it.

Graph mirror: node only, no edge — and why the edge is deferred, not silent
--------------------------------------------------------------------------------
After the authoritative row write, in the SAME transaction, this module
mirrors one ``INVENTORY_RMA`` node — labelled ``InventoryRma:{rma_ref}``,
matching ``StockLocation:{id}`` / ``InventoryItem:{sku}:{id}``'s convention —
guarded by ``nce.entity_resolution.ownership.assert_owner`` (Contract A,
deny-by-default; ``node-ownership.json`` registers ``INVENTORY_RMA`` under
``owner_engine: "inventory"``, Batch 130a). The guarded upsert is this
module's OWN small copy of ``stock.py::_upsert_kg_node``'s shape — it is not
imported across modules, and ``stock.py`` is not refactored to expose it
(Batch 071a's explicit precedent: "write your own small guarded upsert ...
two copies is not yet a refactor trigger").

No ``kg_edges`` row is written. An ``INVENTORY_RMA -[at]-> STOCK_LOCATION``
edge would need the ``StockLocation:{id}`` node to exist, which only a stock
movement creates today (this module never calls one) — so that edge is
DEFERRED, declared here rather than left to be silently missing, and belongs
to whichever wave first needs to traverse it.

Ordering, inherited from B130 and extended by Batch 138b: ``inventory_rma`` →
``kg_nodes``, never the reverse. The authoritative row is written before its
projection, always — a refused mirror (``OwnershipError``, unseeded
namespace) rolls back the whole transaction, INCLUDING the ``inventory_rma``
row this function just wrote, not merely the mirror.

Dependency direction (uncle-bob-craft)
-------------------------------------------
This module imports ``asyncpg``, ``nce.db_utils.scoped_pg_session``,
``nce.entity_resolution.ownership.assert_owner``,
``nce.events.emit.emit_graph_write`` and — Batch 138b's two stock legs —
``nce.vertical_modules.inventory.stock``'s public ``decrement_on_hand`` /
``increment_on_hand`` / ``mirror_item_at_location`` plus
``nce.vertical_modules.inventory.transactions``'s ``append_transaction`` /
``REASON_ADJUSTMENT``. Both are peers within the same
``nce.vertical_modules.inventory`` package — uncle-bob-craft's rule for this
module is "imports only within ``nce.vertical_modules.inventory`` plus
``nce.db_utils``/``nce.events``/``nce.entity_resolution``", not "no sibling
modules at all" — no web/HTTP/admin framework imports either way.
``NCEEngine`` is imported under ``TYPE_CHECKING`` only, matching ``stock.py``
and ``transactions.py``'s own convention.

Decimal coercion is duplicated from transactions.py, not imported
------------------------------------------------------------------------
``transactions.py``'s ``_as_decimal``/``_quantise_qty`` are module-private
(leading underscore); this module carries its own small, adapted copies
rather than importing them across modules — the same duplication-over-
cross-module-private-import choice ``transactions.py``'s own docstring
argues for regarding ``stock.py``.

Batch 138b: the two stock legs this wave adds
------------------------------------------------
Everything above describes ``do_record_rma`` exactly as Batch 138 shipped
it — it still records only, still moves no stock. This wave adds two
SEPARATE public functions, :func:`do_restock_from_rma` and
:func:`do_dispose_rma_weee`, which perform the movement ``do_record_rma``
explicitly declines to: each claims one ``'pending'`` ``inventory_rma`` row
with a single ``UPDATE ... RETURNING`` (:func:`_claim_rma` — the
``AND stock_movement_state = 'pending'`` predicate IS the idempotency
guard), moves ``inventory_items`` through ``stock.py``'s existing guarded
primitives, appends one typed ``inventory_transactions`` row
(``REASON_ADJUSTMENT`` for both legs — see :func:`_run_rma_leg`'s docstring
for why a dedicated category is DEFERRED, not silent), and refreshes the
graph projection — all inside the SAME transaction, in the fixed order
``inventory_rma`` -> ``inventory_items`` -> ``inventory_transactions`` ->
``kg_nodes``. The disposal leg is the one where nothing arrives: stock
leaves permanently and the ledger row is what proves it happened.

Registration is deliberately NOT this wave's job
-----------------------------------------------------
Neither ``do_record_rma`` nor Batch 138b's ``do_restock_from_rma`` /
``do_dispose_rma_weee`` is registered as an MCP tool or a REST route here.
Batch 138a (``inventory-surface-completion``) owns registration for all of
Inventory's ``do_*`` entry points; registration and certification are kept
in different waves on purpose.
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
from nce.vertical_modules.inventory.stock import (
    decrement_on_hand,
    increment_on_hand,
    mirror_item_at_location,
)
from nce.vertical_modules.inventory.transactions import (
    REASON_ADJUSTMENT,
    append_transaction,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.rma")

# ---------------------------------------------------------------------------
# WEEE compliance lifecycle — migration 053's weee_state CHECK, mirrored here
# for a clearer domain error than a raw asyncpg.CheckViolationError.
# ---------------------------------------------------------------------------

WEEE_NOT_APPLICABLE = "not_applicable"
WEEE_PENDING = "pending"
WEEE_AWAITING_COLLECTION = "awaiting_collection"
WEEE_DISPOSED = "disposed"

_VALID_WEEE_STATES: frozenset[str] = frozenset(
    {WEEE_NOT_APPLICABLE, WEEE_PENDING, WEEE_AWAITING_COLLECTION, WEEE_DISPOSED}
)

# This module writes ONLY this value to stock_movement_state — Batch 138b
# performs both transitions ('restocked' / 'disposed'). There is no
# transition function in this module, by construction (see module docstring).
STOCK_MOVEMENT_PENDING = "pending"

# Engine-authored write, not an external-system sync — mirrors stock.py's /
# transactions.py's own 'agent' choice (never 'sync', reserved for the D365
# external-sync origin).
_DEFAULT_CHANGE_ORIGIN = "agent"

# Must equal node-ownership.json's "owner_engine" for INVENTORY_RMA exactly.
_OWNER_ENGINE = "inventory"
_NODE_TYPE_INVENTORY_RMA = "INVENTORY_RMA"

# ---------------------------------------------------------------------------
# Decimal coercion — Decimal end-to-end, quantised BEFORE binding to any
# query (see module docstring's "Decimal coercion is duplicated" section).
# ---------------------------------------------------------------------------

_QTY_SCALE: Decimal = Decimal("0.001")
_ZERO_QTY: Decimal = Decimal("0.000")


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


def _as_decimal(value: Any, where: str) -> Decimal:
    """Coerce a caller-supplied number to an exact, finite ``Decimal``.

    ``bool`` is rejected before the ``int`` branch (``isinstance(True, int)``
    is ``True`` in Python); a float is converted via ``Decimal(str(x))``,
    never ``Decimal(x)`` — the latter imports the binary-float representation
    error (see ``stock.py``'s "Quantity precision" docstring section)."""
    if isinstance(value, bool):
        raise ValueError(f"{where}: bool is not a number, got {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        try:
            candidate = Decimal(str(value))
        except DecimalException as exc:  # pragma: no cover - str(float) always parses
            raise ValueError(f"{where}: not a usable number: {value!r}") from exc
    else:
        raise ValueError(
            f"{where}: expected int/float/Decimal, got {type(value).__name__} {value!r}"
        )
    if not candidate.is_finite():
        raise ValueError(f"{where}: must be finite, got {value!r}")
    return candidate


def _quantise_qty(value: Decimal, where: str) -> Decimal:
    """Round to inventory_rma.qty's own column scale (3dp), ties away from
    zero — same scale and rounding as ``inventory_items.qty_on_hand`` /
    ``inventory_transactions.delta``."""
    try:
        return value.quantize(_QTY_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: quantity is too large to express to 3dp: {value!r}") from exc


def _as_required_text(raw: Any, field: str) -> str:
    """Coerce a caller-supplied value to a required, non-empty, stripped
    string. Used for ``rma_ref``/``reason`` — free-form caller text that
    ``transactions.py`` has no precedent for validating (it never accepts
    either)."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"do_record_rma: '{field}' is required")
    return text


def _as_optional_text(raw: Any) -> str | None:
    """``serial``/``disposal_ref`` are nullable — an absent or blank value is
    ``None``, never an empty string, so a direct-INSERT-style read-back and a
    do_record_rma-written row are indistinguishable."""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


# ---------------------------------------------------------------------------
# Validators — Python-side mirrors of migration 053's CHECK constraints, for
# a clearer domain error than a raw asyncpg.CheckViolationError.
# ---------------------------------------------------------------------------


def _assert_weee_state(weee_state: str) -> None:
    """Mirrors migration 053's ``weee_state`` CHECK."""
    if weee_state not in _VALID_WEEE_STATES:
        raise ValueError(
            f"do_record_rma: weee_state must be one of {sorted(_VALID_WEEE_STATES)}, "
            f"got {weee_state!r}"
        )


def _assert_disposal_ref_present_when_disposed(weee_state: str, disposal_ref: str | None) -> None:
    """Mirrors migration 053's ``inventory_rma_disposed_requires_ref`` CHECK —
    the compliance claim of this wave: a WEEE item cannot be recorded as
    disposed without the take-back scheme's documentation reference."""
    if weee_state == WEEE_DISPOSED and not disposal_ref:
        raise ValueError(
            "do_record_rma: weee_state='disposed' requires a non-empty "
            "disposal_ref (the take-back scheme's documentation reference)"
        )


# ---------------------------------------------------------------------------
# Graph mirror — own small guarded upsert (module docstring's "Graph mirror"
# section). NOT imported from stock.py; NOT stock.py refactored to expose it.
# ---------------------------------------------------------------------------


def _inventory_rma_label(rma_ref: str) -> str:
    return f"InventoryRma:{rma_ref}"


async def _upsert_inventory_rma_kg_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
) -> None:
    """Upsert the one ``INVENTORY_RMA`` kg_nodes row and emit the
    transactional outbox event.

    Guarded by ``assert_owner`` (deny-by-default when no registry row
    exists — Contract A), checked FIRST so a refusal writes nothing at all —
    including, because this runs inside the caller's transaction, the
    ``inventory_rma`` row written just before it (see module docstring's
    ordering section).

    Node only — no ``kg_edges`` row (see module docstring)."""
    await assert_owner(conn, ns_uuid, _NODE_TYPE_INVENTORY_RMA, _OWNER_ENGINE)

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
        _NODE_TYPE_INVENTORY_RMA,
        str(ns_uuid),
        _DEFAULT_CHANGE_ORIGIN,
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_INVENTORY_RMA,
        op="upserted",
        node_id=label,
    )


# ---------------------------------------------------------------------------
# Public: do_record_rma — the SOLE writer of inventory_rma. INSERT-only,
# never UPDATE (see module docstring).
# ---------------------------------------------------------------------------


async def do_record_rma(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Record a return with its WEEE disposal compliance state.

    This function moves NO stock: it writes no ``inventory_transactions``
    row, calls nothing in ``stock.py``, and leaves ``inventory_items``
    untouched (see module docstring). ``stock_movement_state`` is written as
    ``'pending'`` and nothing else — Batch 138b owns both stock legs.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,       # required
            "rma_ref":      str,               # required — the natural key
            "sku":          str,               # required
            "location":     str | UUID,        # required, a stock_locations id
            "qty":          int | float | Decimal,  # required, quantised to 3dp, > 0
            "reason":       str,               # required, free-form
            "serial":       str | None,        # optional — serialised units only
            "weee_state":   str,               # optional, default "not_applicable"
            "disposal_ref": str | None,        # optional; required when weee_state="disposed"
        }``

    Returns
    -------
    dict
        ``{"ok": True, "created": bool, "rma_id": str, "rma_ref": str,
        "sku": str, "location_id": str, "qty": Decimal, "weee_state": str,
        "disposal_ref": str | None, "stock_movement_state": "pending"}``.
        ``created`` is ``False`` when ``rma_ref`` already existed for this
        namespace — the EXISTING row is returned, unmodified (no update path
        in this module).

    Raises
    ------
    ValueError
        Any required field missing/malformed, an unknown ``weee_state``, or
        ``weee_state='disposed'`` with no ``disposal_ref`` — the Python
        mirror of migration 053's ``inventory_rma_disposed_requires_ref``
        CHECK.
    nce.entity_resolution.ownership.OwnershipError
        The namespace's ``node_ownership_registry`` has no (or a
        conflicting) row for ``INVENTORY_RMA`` — the whole transaction rolls
        back, including the ``inventory_rma`` row this call would otherwise
        have written.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    rma_ref = _as_required_text(params.get("rma_ref"), "rma_ref")
    sku = _as_sku(params.get("sku"), "do_record_rma")
    location_id = _as_location_uuid(params.get("location"), "do_record_rma")

    qty = _quantise_qty(_as_decimal(params.get("qty"), "qty"), "qty")
    if qty <= _ZERO_QTY:
        raise ValueError(f"do_record_rma: qty must be > 0, got {qty}")

    reason = _as_required_text(params.get("reason"), "reason")
    serial = _as_optional_text(params.get("serial"))

    weee_state = str(params.get("weee_state") or WEEE_NOT_APPLICABLE).strip()
    _assert_weee_state(weee_state)
    disposal_ref = _as_optional_text(params.get("disposal_ref"))
    _assert_disposal_ref_present_when_disposed(weee_state, disposal_ref)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        inserted = await conn.fetchrow(
            """
            INSERT INTO inventory_rma
                (namespace_id, rma_ref, sku, serial, location_id, qty, reason,
                 weee_state, disposal_ref, stock_movement_state, change_origin)
            VALUES ($1::uuid, $2, $3, $4, $5::uuid, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (namespace_id, rma_ref) DO NOTHING
            RETURNING id, rma_ref, sku, serial, location_id, qty, reason,
                      weee_state, disposal_ref, stock_movement_state
            """,
            str(ns_uuid),
            rma_ref,
            sku,
            serial,
            str(location_id),
            qty,
            reason,
            weee_state,
            disposal_ref,
            STOCK_MOVEMENT_PENDING,
            _DEFAULT_CHANGE_ORIGIN,
        )

        created = inserted is not None
        row = inserted
        if row is None:
            # No update path in this module (see docstring) — a caller
            # re-recording the same rma_ref gets back the EXISTING row,
            # byte-identical, never a fresh write.
            row = await conn.fetchrow(
                """
                SELECT id, rma_ref, sku, serial, location_id, qty, reason,
                       weee_state, disposal_ref, stock_movement_state
                FROM inventory_rma
                WHERE namespace_id = $1::uuid AND rma_ref = $2
                """,
                str(ns_uuid),
                rma_ref,
            )
        assert row is not None  # RETURNING or the fallback SELECT always yields one

        # inventory_rma -> kg_nodes, never the reverse (module docstring's
        # ordering section) — the authoritative row above is already written
        # by the time this runs, so a refusal here rolls the whole
        # transaction back, that row included.
        await _upsert_inventory_rma_kg_node(conn, ns_uuid, _inventory_rma_label(row["rma_ref"]))

    return {
        "ok": True,
        "created": created,
        "rma_id": str(row["id"]),
        "rma_ref": row["rma_ref"],
        "sku": row["sku"],
        "location_id": str(row["location_id"]),
        "qty": row["qty"],
        "weee_state": row["weee_state"],
        "disposal_ref": row["disposal_ref"],
        "stock_movement_state": row["stock_movement_state"],
    }


# ---------------------------------------------------------------------------
# Batch 138b — the two stock legs. See module docstring's "Batch 138b: the
# two stock legs this wave adds" section.
# ---------------------------------------------------------------------------

_STATE_RESTOCKED = "restocked"
_STATE_DISPOSED = "disposed"


class RmaNotFoundError(Exception):
    """No ``inventory_rma`` row exists for this ``(namespace_id, rma_ref)``."""

    def __init__(self, *, rma_ref: str) -> None:
        self.rma_ref = rma_ref
        super().__init__(f"no inventory_rma row for rma_ref={rma_ref!r}")


class RmaAlreadySettledError(Exception):
    """The RMA's ``stock_movement_state`` is no longer ``'pending'`` — a
    stock leg (this call, or a concurrent one) already claimed it."""

    def __init__(self, *, rma_ref: str, stock_movement_state: str) -> None:
        self.rma_ref = rma_ref
        self.stock_movement_state = stock_movement_state
        super().__init__(
            f"rma_ref={rma_ref!r} is already settled: "
            f"stock_movement_state={stock_movement_state!r}, not 'pending'"
        )


class RmaNotWeeeScopeError(Exception):
    """Disposal was attempted on an RMA whose ``weee_state`` is
    ``'not_applicable'`` — a contradiction: no WEEE take-back can be
    documented for an item that is not WEEE-scope."""

    def __init__(self, *, rma_ref: str) -> None:
        self.rma_ref = rma_ref
        super().__init__(
            f"rma_ref={rma_ref!r} has weee_state='not_applicable': cannot "
            "dispose an item that is not WEEE-scope"
        )


async def _claim_rma(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    rma_ref: str,
    *,
    target_state: str,
    disposal_ref: str | None = None,
) -> asyncpg.Record:  # type: ignore[type-arg]
    """Claim one ``inventory_rma`` row for a stock leg — a single
    ``UPDATE ... RETURNING``, never a Python-side read-then-write. This is
    ``stock.py``'s ``decrement_on_hand``'s exact shape and reasoning, applied
    to the RMA's own state machine instead of a quantity column.

    The ``AND stock_movement_state = 'pending'`` predicate IS the idempotency
    guard: two concurrent callers cannot both move the same RMA's stock,
    because only one ``UPDATE`` can see the row as ``'pending'`` — the other
    sees zero rows affected and this raises :class:`RmaAlreadySettledError`.

    The disposal leg (``target_state == 'disposed'``) additionally sets
    ``weee_state = 'disposed'`` and ``disposal_ref`` on the SAME row, and
    guards ``AND weee_state <> 'not_applicable'`` — disposing an item that is
    not WEEE-scope is a contradiction, not a race.

    Returns
    -------
    asyncpg.Record
        ``id``, ``sku``, ``location_id``, ``qty`` from the just-claimed row —
        the caller reads the movement's sku/location/qty from THIS record,
        never from its own ``params``, so the movement can never disagree
        with the record it settles.

    Raises
    ------
    RmaNotFoundError
        No row exists for this ``(namespace_id, rma_ref)``.
    RmaAlreadySettledError
        The row exists but ``stock_movement_state`` is not ``'pending'``.
    RmaNotWeeeScopeError
        ``target_state == 'disposed'`` and the row's ``weee_state`` is
        ``'not_applicable'``.
    """
    if target_state == _STATE_DISPOSED:
        row = await conn.fetchrow(
            """
            UPDATE inventory_rma
            SET stock_movement_state = $3, updated_at = now(),
                weee_state = 'disposed', disposal_ref = $4
            WHERE namespace_id = $1::uuid AND rma_ref = $2
              AND stock_movement_state = 'pending'
              AND weee_state <> 'not_applicable'
            RETURNING id, sku, location_id, qty
            """,
            str(ns_uuid),
            rma_ref,
            target_state,
            disposal_ref,
        )
    else:
        row = await conn.fetchrow(
            """
            UPDATE inventory_rma
            SET stock_movement_state = $3, updated_at = now()
            WHERE namespace_id = $1::uuid AND rma_ref = $2
              AND stock_movement_state = 'pending'
            RETURNING id, sku, location_id, qty
            """,
            str(ns_uuid),
            rma_ref,
            target_state,
        )
    if row is not None:
        return row

    # Guard failed. A diagnostic-only read (no decision hinges on it — the
    # refusal above is already final) to report WHICH of the three cases
    # this was.
    diag = await conn.fetchrow(
        """
        SELECT stock_movement_state, weee_state
        FROM inventory_rma
        WHERE namespace_id = $1::uuid AND rma_ref = $2
        """,
        str(ns_uuid),
        rma_ref,
    )
    if diag is None:
        raise RmaNotFoundError(rma_ref=rma_ref)
    if target_state == _STATE_DISPOSED and diag["weee_state"] == WEEE_NOT_APPLICABLE:
        raise RmaNotWeeeScopeError(rma_ref=rma_ref)
    raise RmaAlreadySettledError(rma_ref=rma_ref, stock_movement_state=diag["stock_movement_state"])


async def _run_rma_leg(
    engine: NCEEngine,
    params: dict[str, Any],
    *,
    target_state: str,
    sign: int,
    disposal_ref: str | None,
) -> dict[str, Any]:
    """Shared body for both stock legs — the two legs are near-identical
    (claim, move, ledger, mirror) and differ only in DIRECTION (``sign``) and
    the claimed target state; this is the one small helper uncle-bob-craft's
    "third duplication" rule allows, and is as far as the abstraction goes
    (no strategy object).

    **Reason category is ``REASON_ADJUSTMENT`` for BOTH legs.** It is B139's
    one open-signed category, so both ``+qty`` (restock) and ``-qty``
    (disposal) satisfy ``inventory_transactions_sign_matches_category``. A
    dedicated ``return_restock`` / ``weee_disposal`` category is DEFERRED,
    declared here rather than silent: widening that CHECK needs a migration,
    and this wave has no allocated number (see module docstring's "Batch
    138b" section) — inventing one is the exact race pre-allocation exists to
    prevent. The two legs stay distinguishable in the ledger by SIGN plus the
    ``ref=f"rma:{rma_ref}"`` attribution.

    One transaction: the claim, the quantity move, the ledger append and the
    mirror all run on the SAME ``conn`` inside ONE ``scoped_pg_session`` block
    — either the whole leg commits or none of it does, by construction, never
    by convention (``transactions.py``'s own words).

    Write order (fixed for both legs and every future RMA leg): ``inventory_
    rma`` -> ``inventory_items`` -> ``inventory_transactions`` -> ``kg_nodes``
    — extends B130's rule (items before kg_nodes) with the RMA claim in
    front. Each leg touches exactly ONE location, so ``_canonical_lock_
    order`` is not needed here and is not called. The mirror is written LAST
    and is never read back as truth (stock.py's own rule).
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    rma_ref = _as_required_text(params.get("rma_ref"), "rma_ref")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        claimed = await _claim_rma(
            conn, ns_uuid, rma_ref, target_state=target_state, disposal_ref=disposal_ref
        )
        sku: str = claimed["sku"]
        location_id: UUID = claimed["location_id"]
        qty: Decimal = claimed["qty"]

        if sign > 0:
            on_hand = await increment_on_hand(conn, ns_uuid, sku, location_id, qty)
        else:
            on_hand = await decrement_on_hand(conn, ns_uuid, sku, location_id, qty)

        await append_transaction(
            conn,
            ns_uuid,
            sku=sku,
            location_id=location_id,
            delta=qty if sign > 0 else -qty,
            reason_category=REASON_ADJUSTMENT,
            ref=f"rma:{rma_ref}",
            unit_cost=None,
        )

        await mirror_item_at_location(conn, ns_uuid, sku, location_id)

    return {
        "rma_ref": rma_ref,
        "sku": sku,
        "location_id": str(location_id),
        "qty": qty,
        "on_hand": on_hand,
    }


async def do_restock_from_rma(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Return a repairable unit to stock at the RMA's own location.

    Parameters
    ----------
    params:
        ``{"namespace_id": str | UUID, "rma_ref": str}`` — ``sku``,
        ``location_id`` and ``qty`` are read from the CLAIMED
        ``inventory_rma`` row, never from *params* (see :func:`_run_rma_leg`).

    Returns
    -------
    dict
        ``{"ok": True, "rma_ref", "sku", "location_id", "qty", "on_hand",
        "stock_movement_state": "restocked"}``.

    Raises
    ------
    ValueError
        ``namespace_id``/``rma_ref`` missing or malformed.
    RmaNotFoundError
        No ``inventory_rma`` row for this ``rma_ref``.
    RmaAlreadySettledError
        The RMA's ``stock_movement_state`` is not ``'pending'`` — including a
        second call for an RMA this same function already restocked.
    OwnershipError
        The graph mirror's ``assert_owner`` check refuses the write; rolls
        back the whole leg, the claim and the quantity move included.
    """
    result = await _run_rma_leg(
        engine, params, target_state=_STATE_RESTOCKED, sign=1, disposal_ref=None
    )
    return {
        "ok": True,
        "rma_ref": result["rma_ref"],
        "sku": result["sku"],
        "location_id": result["location_id"],
        "qty": result["qty"],
        "on_hand": result["on_hand"],
        "stock_movement_state": _STATE_RESTOCKED,
    }


async def do_dispose_rma_weee(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Permanently remove a WEEE-scope return from stock under the approved
    take-back scheme — the leg nothing arrives for: stock leaves and the
    ledger row is what proves it happened.

    Parameters
    ----------
    params:
        ``{"namespace_id": str | UUID, "rma_ref": str, "disposal_ref": str}``
        — ``disposal_ref`` is REQUIRED (the take-back scheme's documentation
        reference); ``sku``, ``location_id`` and ``qty`` are read from the
        CLAIMED ``inventory_rma`` row, never from *params*.

    Returns
    -------
    dict
        ``{"ok": True, "rma_ref", "sku", "location_id", "qty", "on_hand",
        "disposal_ref", "weee_state": "disposed",
        "stock_movement_state": "disposed"}``.

    Raises
    ------
    ValueError
        ``namespace_id``/``rma_ref`` missing/malformed, or no non-empty
        ``disposal_ref`` given — the Python mirror of migration 053's
        ``inventory_rma_disposed_requires_ref`` CHECK, checked BEFORE any DB
        call.
    RmaNotFoundError
        No ``inventory_rma`` row for this ``rma_ref``.
    RmaNotWeeeScopeError
        The RMA's ``weee_state`` is ``'not_applicable'`` — disposing an item
        that is not WEEE-scope is a contradiction.
    RmaAlreadySettledError
        The RMA's ``stock_movement_state`` is not ``'pending'``.
    InsufficientStockError
        The claimed location does not hold at least ``qty`` units of ``sku``
        — rolls back the whole leg, the claim included.
    OwnershipError
        The graph mirror's ``assert_owner`` check refuses the write; rolls
        back the whole leg, the claim and the quantity move included.
    """
    disposal_ref = _as_optional_text(params.get("disposal_ref"))
    _assert_disposal_ref_present_when_disposed(WEEE_DISPOSED, disposal_ref)

    result = await _run_rma_leg(
        engine, params, target_state=_STATE_DISPOSED, sign=-1, disposal_ref=disposal_ref
    )
    return {
        "ok": True,
        "rma_ref": result["rma_ref"],
        "sku": result["sku"],
        "location_id": result["location_id"],
        "qty": result["qty"],
        "on_hand": result["on_hand"],
        "disposal_ref": disposal_ref,
        "weee_state": WEEE_DISPOSED,
        "stock_movement_state": _STATE_DISPOSED,
    }
