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
This module imports only ``asyncpg``, ``nce.db_utils.scoped_pg_session``,
``nce.entity_resolution.ownership.assert_owner`` and
``nce.events.emit.emit_graph_write`` — no web/HTTP/admin framework imports,
and nothing from another vertical module (in particular, nothing from
``nce.vertical_modules.inventory.stock`` or ``.transactions``).
``NCEEngine`` is imported under ``TYPE_CHECKING`` only, matching ``stock.py``
and ``transactions.py``'s own convention.

Decimal coercion is duplicated from transactions.py, not imported
------------------------------------------------------------------------
``transactions.py``'s ``_as_decimal``/``_quantise_qty`` are module-private
(leading underscore); this module carries its own small, adapted copies
rather than importing them across modules — the same duplication-over-
cross-module-private-import choice ``transactions.py``'s own docstring
argues for regarding ``stock.py``.

Registration is deliberately NOT this wave's job
-----------------------------------------------------
``do_record_rma`` is not registered as an MCP tool or a REST route here.
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
