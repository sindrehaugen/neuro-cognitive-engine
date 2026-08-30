"""
nce/vertical_modules/inventory/reservation.py
================================================
The reservation primitive (Module 11, Wave 8 — Batch 136, RE-SCOPED):
``do_reserve_stock`` / ``do_release_stock`` over migration 050's
``inventory_items`` row — moving ``qty_reserved``, never ``qty_on_hand``.

**Scope override, stated explicitly because the original wave brief named a
wider goal:** the original Wave 8 brief was "``do_reserve_stock`` +
phantom-BOM kitting". Kitting is a SEPARATE concern (BOM traversal against a
kit representation that does not exist anywhere in this codebase yet) and is
explicitly OUT OF SCOPE here — it is Batch 136b, blocked until a kit
representation is named. This module contains **no BOM traversal, no kit
expansion, no phantom-BOM logic of any kind**. If a future change to this
file is tempted to add any of that, it must STOP and get a kit representation
named first, not improvise one here.

The reservation algebra — which term this module moves
-----------------------------------------------------------
``available = qty_on_hand - qty_reserved - qty_blocked`` (roadmap's A2,
adopted verbatim — see ``stock.py``'s module docstring, "The reservation
algebra"). ``stock.py``'s ``do_transfer_stock`` / ``do_record_consumption``
move ``qty_on_hand`` — the physical stock actually moves. This module moves
the OTHER term: :func:`do_reserve_stock` increments ``qty_reserved``,
:func:`do_release_stock` decrements it. ``qty_on_hand`` is never written by
this module — a reservation does not move physical stock, it only narrows
how much of the stock already on hand is still up for grabs.

The concurrency contract: ONE guarded UPDATE, refused by Postgres
----------------------------------------------------------------------
Exactly Batch 130's proven pattern (``stock.py``'s ``_decrement_on_hand``),
generalised from the two-term ``qty_on_hand >= n`` guard to the full
three-term identity:

  * :func:`do_reserve_stock` — ``UPDATE inventory_items SET qty_reserved =
    qty_reserved + $qty WHERE qty_on_hand - qty_reserved - qty_blocked >=
    $qty``. The guard (enough is AVAILABLE) and the write (reserve it)
    happen in ONE statement, evaluated atomically under the row's own lock
    — never a Python-side "read available, check in Python, then UPDATE",
    which races under concurrent callers exactly as Batch 130's docstring
    argues at length. A racing reservation that would drive ``available``
    negative instead affects **zero rows**, and this module raises
    :class:`InsufficientAvailableError` rather than assuming success.
  * :func:`do_release_stock` — ``UPDATE inventory_items SET qty_reserved =
    qty_reserved - $qty WHERE qty_reserved >= $qty``, the same one-statement
    shape, refusing to release more than is currently reserved.

Negative reservations are structurally impossible via TWO independent
mechanisms (belt and braces, matching ``stock.py``'s own reasoning for
``qty_on_hand``): this module's own guard refuses the write before
``qty_reserved`` would exceed what is available (reserve) or go negative
(release); migration 050's ``CHECK (qty_reserved >= 0)`` on the column itself
would refuse it even if this module's guard were ever bypassed. Note what is
NOT independently re-guarded: nothing in the schema enforces ``qty_on_hand -
qty_reserved - qty_blocked >= 0`` as a table CHECK (migration 050 has none),
so this module's own WHERE clause is the ONLY thing standing between a
concurrent caller and over-reservation — which is exactly why the guard must
be the single atomic UPDATE described above, not a read-then-write.

``tests/test_inventory_reservation.py`` proves the reserve-side guard with
REAL concurrent connections (``asyncio.gather`` over separate pool
connections), not sequential calls on one connection — a sequential test
cannot distinguish a real atomic guard from a racy read-then-check that
merely happens not to interleave.

Because each call touches exactly ONE ``inventory_items`` row and writes NO
``kg_nodes`` row (see "Graph mirror" below), there is only ever one lockable
resource per call — unlike ``stock.py``'s ``do_transfer_stock`` (two
locations, up to eight lockable resources), a deadlock cannot form here: a
cycle needs at least two resources acquired in different orders by two
transactions, and this module never acquires a second one. No canonical
lock-order helper is needed or introduced; ``stock.py``'s own
``_canonical_lock_order`` is untouched and unused by this module.

Explicit decision: a reservation is NOT a ledger movement
----------------------------------------------------------
**Decision: reservations do not append to ``inventory_transactions``.**
Reasoning: the ledger (``transactions.py``, migration 051) exists to record
movements of *physical* stock — its four typed reason categories
(``transfer_in``, ``transfer_out``, ``consumption``, ``adjustment``) are all
changes to ``qty_on_hand``, and ``append_transaction``'s own sign-check
(``_assert_sign_matches_category``) is written entirely in terms of
``qty_on_hand`` deltas. A reservation changes ``qty_reserved`` while
``qty_on_hand`` — the actual count of physical units sitting on a shelf or in
a van — does not move at all. Forcing a reservation through
``append_transaction`` would mean inventing a fifth reason category with no
``qty_on_hand`` delta to record, which is not what that ledger models and
would silently blur "stock moved" with "stock earmarked". This module
therefore never imports ``nce.vertical_modules.inventory.transactions`` and
never writes ``inventory_transactions``. If a future wave needs an audit
trail of WHO reserved WHAT and WHEN (e.g. for a reservation-expiry Watcher),
that is a new, separate concern from the physical-movement ledger — not a
retrofit of this one.

Graph mirror: an edge only, never a node — and why
------------------------------------------------------
Per the roadmap's graph contract (``docs/vertical_engines/11-inventory-engine.md``
line 40): ``INVENTORY_ITEM -[reserved_for]-> PROJECT``. This module writes
**only that edge**, and only that edge:

* It never upserts a ``PROJECT_PROJECT`` node — ``node-ownership.json``
  already assigns that node type to the Project engine (Contract A,
  deny-by-default); writing it here would be a silent cross-engine
  ownership violation, exactly what Contract A exists to prevent. This
  module writes the edge by ``PROJECT:{...}`` label only, trusting the
  caller-supplied label byte-for-byte (mirrors
  ``project/insights.py``'s ``do_detect_scope_creep``, which does the same
  for a caller-supplied ``project_id``) — the label must be the same one
  ``project/convert.py``'s ``do_convert_signed_quote`` already returned.
* It never upserts an ``INVENTORY_ITEM`` node either, unlike ``stock.py``.
  ``kg_edges`` has no FK to ``kg_nodes`` (confirmed by ``stock.py``'s own
  ``_upsert_kg_edge`` and by ``procurement/graph.py``'s
  ``upsert_offers_edge`` — "cross-engine edge writes by label are legal"),
  so the edge does not need the node to exist first, and this module does
  not need to duplicate ``stock.py``'s node-upsert machinery to write it.
  This also means this module adds NO new unguarded ``kg_nodes`` writer on
  top of the one ``stock.py`` already has flagged as a gap pending Batch
  130a's ``node-ownership.json`` addition — it simply never touches
  ``kg_nodes`` at all.
* ``node-ownership.json`` is Batch 130a's file, in flight on another
  worktree right now; this module does not read it, edit it, or depend on
  its outcome — an edge write needs no ownership check either way (edges
  have none), so nothing here is coupled to when B130a lands.

The edge uses ``change_origin='agent'`` (engine-authored write, not an
external sync — mirrors ``stock.py``'s and ``economy/graph.py``'s own
choice) and ``confidence=1.0`` (written synchronously in the same
transaction as the row update, so it is maximally fresh at the instant it is
written — mirrors ``stock.py``'s ``_FRESH_CONFIDENCE`` reasoning).

Honest scope limits — stated, not silently resolved
---------------------------------------------------------
1. **``qty_reserved`` is a single aggregate column, not decomposed per
   project.** ``inventory_items`` (migration 050) carries one
   ``qty_reserved`` number per ``(namespace, sku, location)`` — it does not
   record which project contributed how much of it. Consequently
   :func:`do_release_stock` CANNOT verify that a caller is releasing only
   the portion it itself reserved; it can only verify that the row's total
   ``qty_reserved`` is large enough to absorb the release. Two different
   projects reserving against the same ``(sku, location)`` and then one
   releasing more than its own share is not something this module can
   detect from the row alone — that is a caller-side accounting invariant,
   not one this primitive enforces. A future wave building a genuine
   per-reservation ledger (one row per reservation, not one aggregate
   column) would close this; it is out of scope for a reservation
   *primitive* over the existing schema, and doing so here would be new DDL
   this wave was told not to invent (Migrations: NONE — see "Hard
   constraints").
2. **The ``reserved_for`` edge is a historical/audit association, not a
   live gauge.** :func:`do_reserve_stock` upserts it; :func:`do_release_stock`
   never deletes or downgrades it. Because ``qty_reserved`` is aggregate
   (limit 1 above), this module has no way to know whether releasing some
   quantity means a project's association with this item has fully ended —
   another reservation for the SAME project against the SAME row may still
   be outstanding, or may be made again later. Treating the edge as "this
   project has, at some point, reserved this item" (append-only-ish, never
   retracted) is the only claim this module can make honestly; treating its
   absence-after-release as "this project no longer needs this item" would
   overclaim what the aggregate column can support.

Dependency direction (uncle-bob-craft)
-----------------------------------------
This module imports only ``asyncpg`` and ``nce.db_utils.scoped_pg_session`` —
no web/HTTP/admin framework imports, and (deliberately) nothing from
``stock.py`` or ``transactions.py``: the small coercion helpers below
(``_as_ns_uuid``, ``_as_sku``, ``_as_location_uuid``, ``_as_quantity``,
``_quantise_qty``, ``_inventory_item_label``) are adapted duplicates of
``stock.py``'s own private (leading-underscore) helpers, not cross-module
imports of another module's private API — the same choice
``transactions.py`` already documents making for its own copies of the same
helpers ("Decimal coercion is duplicated from stock.py, not imported").
``NCEEngine`` is imported under ``TYPE_CHECKING`` only, matching both
sibling modules' convention.

Namespace scoping
---------------------
Every query's ``WHERE`` clause filters by ``namespace_id`` explicitly. Unlike
``do_stock_levels`` (which can be called with no ``location`` filter, making
a same-sku-cross-namespace collision test meaningful there), every function
in this module requires an explicit ``location`` — a globally-unique
``stock_locations.id`` — so a WHERE-clause-only proof would pass even with
the ``namespace_id`` filter removed and would not be discriminating.
``tests/test_inventory_reservation.py`` therefore proves isolation the way
that is actually load-bearing here: a FORCE-RLS proof driven through a real
``nce_app`` pool, never the superuser ``pg_pool`` fixture (following
``test_inventory_stock.py``'s own precedent) — if ``scoped_pg_session``
failed to set the namespace GUC, the write would be refused outright by
RLS's ``WITH CHECK``, not silently mis-scoped.

Quantity precision — NUMERIC(18,3), Decimal end-to-end
----------------------------------------------------------
``inventory_items.qty_reserved`` is the same ``NUMERIC(18,3)`` column
``qty_on_hand`` is. :func:`_as_quantity` coerces every caller-supplied
quantity to an exact ``Decimal`` via ``Decimal(str(x))`` (never
``Decimal(x)``, which imports the binary-float representation error) and
quantises it to 3dp BEFORE it is bound to any query — identical discipline
to ``stock.py``'s own ``_as_quantity``, duplicated rather than imported per
the "Dependency direction" section above.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.reservation")

# ---------------------------------------------------------------------------
# Graph mirror constants — edge only, see module docstring's "Graph mirror".
# ---------------------------------------------------------------------------

_PRED_RESERVED_FOR = "reserved_for"
_PROJECT_LABEL_PREFIX = "PROJECT:"
# Engine-authored write, not an external-system sync — mirrors stock.py's
# and economy/graph.py's own 'agent' choice.
_CHANGE_ORIGIN = "agent"
# Written synchronously in the same transaction as the row update it
# reflects — maximally fresh at the instant it is written (stock.py's
# _FRESH_CONFIDENCE reasoning).
_FRESH_CONFIDENCE = 1.0

# ---------------------------------------------------------------------------
# Quantity coercion — duplicated from stock.py, not imported (see module
# docstring's "Dependency direction").
# ---------------------------------------------------------------------------

_QTY_SCALE: Decimal = Decimal("0.001")
_ZERO_QTY: Decimal = Decimal("0.000")


def _quantise_qty(value: Decimal, where: str) -> Decimal:
    """Round *value* to inventory_items' own column scale (3dp), ties away
    from zero. Duplicated verbatim from stock.py's helper of the same name."""
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


def _as_project_label(raw: Any, where: str) -> str:
    """Validate and return a caller-supplied ``PROJECT:{...}`` label.

    This module never constructs the label itself (it does not know a
    quote_id) — it trusts the caller-supplied label byte-for-byte, the same
    convention ``project/insights.py``'s ``do_detect_scope_creep`` already
    uses for its own ``project_id`` parameter. Only the ``PROJECT:`` prefix
    and a non-empty remainder are checked; casing of the remainder is not
    normalised (callers are expected to pass through the exact label
    ``project/convert.py``'s ``do_convert_signed_quote`` returned).
    """
    label = str(raw or "").strip()
    if not label:
        raise ValueError(f"{where}: 'project_id' is required")
    if not label.upper().startswith(_PROJECT_LABEL_PREFIX):
        raise ValueError(
            f"{where}: 'project_id' must be a PROJECT label "
            f"(e.g. {_PROJECT_LABEL_PREFIX}XYZ), got {label!r}"
        )
    if len(label) <= len(_PROJECT_LABEL_PREFIX):
        raise ValueError(
            f"{where}: 'project_id' must include a quote id after "
            f"{_PROJECT_LABEL_PREFIX!r}, got {label!r}"
        )
    return label


def _inventory_item_label(sku: str, location_id: UUID) -> str:
    """Duplicated verbatim from stock.py's helper of the same name — the
    canonical INVENTORY_ITEM label, keyed by (sku, location)."""
    return f"InventoryItem:{sku}:{location_id}"


# ---------------------------------------------------------------------------
# Domain errors — business-rule refusals, not input-validation ValueErrors.
# ---------------------------------------------------------------------------


class InsufficientAvailableError(Exception):
    """Raised when :func:`do_reserve_stock`'s ``WHERE qty_on_hand -
    qty_reserved - qty_blocked >= n`` guard affects zero rows — either the
    row exists but does not have enough AVAILABLE stock, or no
    ``inventory_items`` row exists yet for this ``(sku, location)`` (treated
    as ``available=0``, which is the correct current state)."""

    def __init__(
        self,
        *,
        sku: str,
        location_id: UUID,
        project_id: str,
        requested: Decimal,
        on_hand: Decimal,
        reserved: Decimal,
        blocked: Decimal,
    ) -> None:
        self.sku = sku
        self.location_id = location_id
        self.project_id = project_id
        self.requested = requested
        self.on_hand = on_hand
        self.reserved = reserved
        self.blocked = blocked
        self.available = on_hand - reserved - blocked
        super().__init__(
            f"insufficient available stock for sku={sku!r} at location={location_id} "
            f"(project={project_id!r}): requested {requested}, only {self.available} "
            f"available (on_hand={on_hand}, reserved={reserved}, blocked={blocked})"
        )


class OverReleaseError(Exception):
    """Raised when :func:`do_release_stock`'s ``WHERE qty_reserved >= n``
    guard affects zero rows — releasing more than is currently reserved at
    this ``(sku, location)`` (or no row exists at all, treated as
    ``reserved=0``)."""

    def __init__(
        self,
        *,
        sku: str,
        location_id: UUID,
        project_id: str,
        requested: Decimal,
        currently_reserved: Decimal,
    ) -> None:
        self.sku = sku
        self.location_id = location_id
        self.project_id = project_id
        self.requested = requested
        self.currently_reserved = currently_reserved
        super().__init__(
            f"cannot release {requested} of sku={sku!r} at location={location_id} "
            f"(project={project_id!r}): only {currently_reserved} is currently reserved"
        )


# ---------------------------------------------------------------------------
# Graph mirror — edge only, no node upsert (see module docstring).
# ---------------------------------------------------------------------------


async def _upsert_reserved_for_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    sku: str,
    location_id: UUID,
    project_label: str,
) -> None:
    """Upsert ``INVENTORY_ITEM -[reserved_for]-> PROJECT_PROJECT`` in
    ``kg_edges`` only. No ``kg_nodes`` row is written by this module for
    either endpoint — ``kg_edges`` has no FK to ``kg_nodes``, so this write
    is always safe regardless of whether either endpoint's node exists yet
    (mirrors ``stock.py``'s ``_upsert_kg_edge`` / ``procurement/graph.py``'s
    ``upsert_offers_edge`` reasoning)."""
    subject = _inventory_item_label(sku, location_id)
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
        subject,
        _PRED_RESERVED_FOR,
        project_label,
        _FRESH_CONFIDENCE,
        str(ns_uuid),
        _CHANGE_ORIGIN,
    )


# ---------------------------------------------------------------------------
# Public: do_reserve_stock — increments qty_reserved, guarded by `available`.
# ---------------------------------------------------------------------------


async def do_reserve_stock(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Reserve *qty* units of *sku* at *location* against *project_id*.

    Increments ``inventory_items.qty_reserved`` — NEVER ``qty_on_hand``, no
    physical stock moves (see module docstring's "reservation algebra"
    section). The guard and the write are ONE statement (``UPDATE ... WHERE
    qty_on_hand - qty_reserved - qty_blocked >= $qty``), refused by Postgres
    under the row's own lock — never a Python-side read-then-check.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "sku":          str,          # required
            "qty":          int | float | Decimal,  # required, > 0
            "location":     str | UUID,   # required, a stock_locations id
            "project_id":   str,          # required, e.g. "PROJECT:QUOTE-001"
        }``

        ``project_id`` must be the exact label ``project/convert.py``'s
        ``do_convert_signed_quote`` returned — this module trusts it
        byte-for-byte (see module docstring's "Graph mirror" section).

    Returns
    -------
    dict
        ``{"ok": True, "sku", "location_id", "project_id", "qty", "on_hand",
        "reserved", "blocked", "available"}`` — all four stock fields are the
        POST-reservation values, ``available`` computed as the full
        three-term subtraction (never a shortcut).

    Raises
    ------
    ValueError
        Any required field missing/malformed.
    InsufficientAvailableError
        Fewer than *qty* units are AVAILABLE at *location* for *sku*.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    sku = _as_sku(params.get("sku"), "sku")
    qty = _as_quantity(params.get("qty"), "qty")
    location = _as_location_uuid(params.get("location"), "location")
    project_label = _as_project_label(params.get("project_id"), "do_reserve_stock")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            UPDATE inventory_items
            SET qty_reserved = qty_reserved + $4, updated_at = now()
            WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
              AND qty_on_hand - qty_reserved - qty_blocked >= $4
            RETURNING qty_on_hand, qty_reserved, qty_blocked
            """,
            str(ns_uuid),
            sku,
            str(location),
            qty,
        )

        if row is None:
            # Guard failed. Diagnostic-only read (no decision hinges on it —
            # the refusal is already final) to report the actual current
            # state, or all-zero when no row exists for this (sku, location).
            current = await conn.fetchrow(
                """
                SELECT qty_on_hand, qty_reserved, qty_blocked FROM inventory_items
                WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
                """,
                str(ns_uuid),
                sku,
                str(location),
            )
            raise InsufficientAvailableError(
                sku=sku,
                location_id=location,
                project_id=project_label,
                requested=qty,
                on_hand=current["qty_on_hand"] if current is not None else _ZERO_QTY,
                reserved=current["qty_reserved"] if current is not None else _ZERO_QTY,
                blocked=current["qty_blocked"] if current is not None else _ZERO_QTY,
            )

        on_hand = row["qty_on_hand"]
        reserved = row["qty_reserved"]
        blocked = row["qty_blocked"]

        await _upsert_reserved_for_edge(conn, ns_uuid, sku, location, project_label)

    log.info(
        "do_reserve_stock: ns=%s sku=%s qty=%s location=%s project=%s reserved=%s",
        ns_uuid,
        sku,
        qty,
        location,
        project_label,
        reserved,
    )
    return {
        "ok": True,
        "sku": sku,
        "location_id": str(location),
        "project_id": project_label,
        "qty": qty,
        "on_hand": on_hand,
        "reserved": reserved,
        "blocked": blocked,
        "available": on_hand - reserved - blocked,
    }


# ---------------------------------------------------------------------------
# Public: do_release_stock — decrements qty_reserved, guarded by `reserved`.
# ---------------------------------------------------------------------------


async def do_release_stock(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Release *qty* previously-reserved units of *sku* at *location*.

    Decrements ``inventory_items.qty_reserved`` — NEVER ``qty_on_hand``. The
    guard and the write are ONE statement (``UPDATE ... WHERE qty_reserved
    >= $qty``), refused by Postgres under the row's own lock.

    ``project_id`` is accepted and echoed back for caller symmetry and
    audit-trail logging, but — per module docstring's "Honest scope limits"
    — it does NOT gate the release: ``qty_reserved`` is a single aggregate
    column with no per-project breakdown, so this function cannot verify a
    caller releases only its own prior contribution, only that the row's
    total reserved quantity is large enough to absorb the release. This
    function never deletes or modifies the ``reserved_for`` edge
    :func:`do_reserve_stock` wrote — see module docstring for why.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "sku":          str,          # required
            "qty":          int | float | Decimal,  # required, > 0
            "location":     str | UUID,   # required, a stock_locations id
            "project_id":   str,          # required, e.g. "PROJECT:QUOTE-001"
        }``

    Returns
    -------
    dict
        ``{"ok": True, "sku", "location_id", "project_id", "qty", "on_hand",
        "reserved", "blocked", "available"}`` — all four stock fields are the
        POST-release values.

    Raises
    ------
    ValueError
        Any required field missing/malformed.
    OverReleaseError
        Fewer than *qty* units are currently reserved at *location* for *sku*.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    sku = _as_sku(params.get("sku"), "sku")
    qty = _as_quantity(params.get("qty"), "qty")
    location = _as_location_uuid(params.get("location"), "location")
    project_label = _as_project_label(params.get("project_id"), "do_release_stock")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            UPDATE inventory_items
            SET qty_reserved = qty_reserved - $4, updated_at = now()
            WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
              AND qty_reserved >= $4
            RETURNING qty_on_hand, qty_reserved, qty_blocked
            """,
            str(ns_uuid),
            sku,
            str(location),
            qty,
        )

        if row is None:
            current_reserved = await conn.fetchval(
                """
                SELECT qty_reserved FROM inventory_items
                WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
                """,
                str(ns_uuid),
                sku,
                str(location),
            )
            raise OverReleaseError(
                sku=sku,
                location_id=location,
                project_id=project_label,
                requested=qty,
                currently_reserved=current_reserved if current_reserved is not None else _ZERO_QTY,
            )

        on_hand = row["qty_on_hand"]
        reserved = row["qty_reserved"]
        blocked = row["qty_blocked"]

    log.info(
        "do_release_stock: ns=%s sku=%s qty=%s location=%s project=%s reserved=%s",
        ns_uuid,
        sku,
        qty,
        location,
        project_label,
        reserved,
    )
    return {
        "ok": True,
        "sku": sku,
        "location_id": str(location),
        "project_id": project_label,
        "qty": qty,
        "on_hand": on_hand,
        "reserved": reserved,
        "blocked": blocked,
        "available": on_hand - reserved - blocked,
    }
