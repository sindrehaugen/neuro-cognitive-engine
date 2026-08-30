"""
nce/vertical_modules/inventory/goods_receipt.py
==================================================
Goods-receipt: the idempotent, ledgered, authoritative inbound stock
increment (Module 11, Wave 4 — ``goods-receipt``, Batch 132), migration
052's ``goods_receipts`` table.

Per ``docs/vertical_engines/11-inventory-engine.md`` ("NCE *is* the
inventory system" — there is no external system of record) and
``00-ENGINES-ROADMAP.md`` §2.11/§9.1/§9.5.

**RE-SCOPED 2026-08-17.** This wave used to cover three concerns (the table +
writer, the graph projection, the C4 publish). It now covers ONE: make an
inbound delivery a recorded, ledgered, replay-safe increase in authoritative
stock. See "Explicit out of scope" below for the other two, and the batch
that owns each.

Goal, precisely
------------------
:func:`do_record_goods_receipt` — in **one** ``scoped_pg_session``
transaction — inserts the ``goods_receipts`` row, increments
``inventory_items.qty_on_hand`` for each received line, and appends one
``inventory_transactions`` row per line via
``transactions.py``'s :func:`append_transaction`. A goods receipt is where
unit cost first enters the system: ``inventory_items`` has no cost column
(migration 050's own docstring), so ``unit_cost`` rides on the ledger row —
this is the first writer in the program that supplies a REAL cost rather
than a seeded stand-in.

Idempotency is BY CONSTRUCTION, never a check-then-write
------------------------------------------------------------
``goods_receipts_idempotency_uq`` — a ``UNIQUE (namespace_id, receipt_hash)``
index (migration 052) — is the sole arbiter. The receipt's own INSERT uses
``ON CONFLICT (namespace_id, receipt_hash) DO NOTHING``; when it returns no
row, this IS a replay, and every subsequent effect (stock increment, ledger
append) is gated on that INSERT having returned a row. This is never
re-expressed as a ``SELECT ... THEN INSERT`` pre-check — that shape
reintroduces exactly the race the unique index exists to close (two
concurrent identical submissions would both pass the pre-check and both
apply their side effects). The DB-level refusal is the mechanism; the
Python code only reacts to it.

``stock.py`` is duplicated, not imported from privately — and the ORIGINAL
first reason for doing so was wrong
------------------------------------------------------------------------------
This module needs an atomic increment of ``inventory_items.qty_on_hand``.
``nce/vertical_modules/inventory/stock.py::_increment_on_hand`` already does
exactly this, and this module still writes its OWN copy
(:func:`_increment_qty_on_hand`) of the identical single-statement
``INSERT ... ON CONFLICT (namespace_id, sku, location_id) DO UPDATE SET
qty_on_hand = inventory_items.qty_on_hand + EXCLUDED.qty_on_hand`` shape.

**Correction (2026-08-18).** An earlier revision of this section gave three
reasons and LED with one that was false: that ``stock.py``'s
``ForeignKeyViolationError`` translation hard-codes transfer-flavoured
wording (``"do_transfer_stock: to_location … does not exist"``) this module
would have to re-word. That justification rested on a branch that could
never fire from this module's only call site — the receipt's own INSERT
holds the identical composite FK on ``stock_locations (id, namespace_id)``
and runs FIRST, so a bad ``location_id`` is refused there and
:func:`_increment_qty_on_hand` is never reached. The branch has been DELETED
(not re-worded) and the reason is WITHDRAWN, not swapped for a fresh one.
What honestly remains is two things, both weaker than what they replace:

  1. ``_increment_on_hand`` is module-PRIVATE. Importing it means reaching
     across a module boundary for a leading-underscore name — the SAME
     convention this file already applies to the Decimal coercion helpers
     (last section of this docstring), not a rule invented for this wave.
  2. ``stock.py`` is the CONTESTED file of this module this cycle: B139 added
     ledger appends to it and B130a added an ``assert_owner`` call to it —
     an edit from this wave would be a live collision attributed to the
     wrong batch, and its ``_canonical_lock_order`` plus its
     ``UPDATE ... WHERE qty_on_hand >= $4`` oversell guard must not be
     disturbed (B130's blocking defect was a write that could abort its own
     source of truth and deadlocked at 100% of rounds). This module never
     edits, imports from, or refactors that file.

Neither survives a THIRD occurrence. The honest statement is: this is
duplication TOLERATED for one more copy, on a convention and a scheduling
constraint.

The two bodies are NOT behaviourally identical, and deleting that branch is
what made them differ. Before the deletion both carried an
``asyncpg.ForeignKeyViolationError`` handler and differed only in message
wording; now ``stock.py::_increment_on_hand`` still translates that error
into a ``ValueError`` while :func:`_increment_qty_on_hand` lets the raw
``asyncpg.ForeignKeyViolationError`` propagate. Called DIRECTLY with a
``location_id`` that is not a ``stock_locations`` row, the two helpers raise
different exception TYPES for the same input. The difference is deliberate
and it is scoped to each helper's own caller: ``stock.py``'s branch is
REACHABLE from ``do_transfer_stock`` (nothing there holds the location FK
first), whereas this one's was reachable from nothing —
:func:`do_record_goods_receipt` inserts the receipt, under the textually
identical composite FK on ``stock_locations (id, namespace_id)``, BEFORE it
calls this helper, so a bad location is always refused there with a message.
The branch was dead code in this module and was deleted rather than kept as
coverage theatre; it is NOT re-added, and the duplication rests on the two
reasons above, not on any claim that the bodies behave alike.

The next writer that needs this upsert should extract a shared, PUBLIC
helper and move both existing call sites onto it rather than add copy number
three — and must reconcile this exception-type difference at that point,
since a shared helper cannot have it both ways.

Deterministic per-line ordering — no new deadlock cycle
------------------------------------------------------------
Every line in one receipt lands at the SAME ``location_id`` (there is no
cross-location cycle within a single call, unlike ``stock.py``'s
``do_transfer_stock``), but two CONCURRENT receipts touching an overlapping
SKU set in opposite orders would still deadlock on the ``inventory_items``
row locks if processed in caller-supplied order. :func:`do_record_goods_receipt`
processes its aggregated lines in ascending-``sku`` order — the same
discipline ``stock.py``'s ``_canonical_lock_order`` embodies for
cross-location locks, applied here to cross-line locks within one location.

Hash stability — what is, and is NOT, part of the idempotency key
------------------------------------------------------------------------
:func:`_compute_receipt_hash` hashes a canonically normalised encoding of
``(po_ref, location_id, lines, scans)``, PLUS ``delivery_note_ref`` when — and
only when — one was supplied (an absent note omits the key rather than
hashing as ``null``; see "Two genuinely distinct deliveries" below for why
that conditional presence is load-bearing rather than a stylistic choice) —
``json.dumps(..., sort_keys=True, separators=(",", ":"))``, quantities and
costs rendered as their quantised DECIMAL STRINGS (never floats, which would
make the hash non-deterministic across equivalent binary representations).
``received_at``, ``created_at`` and ``id`` are DELIBERATELY excluded —
including a timestamp would make every retry a new receipt and defeat the
entire mechanism.

Text refs are normalised ONCE, AT THE BOUNDARY — never again in the hash
--------------------------------------------------------------------------
``po_ref`` and ``delivery_note_ref`` are stripped and upper-cased by
:func:`_as_po_ref` / :func:`_as_optional_delivery_note_ref`, and everything
downstream — the hash, the stored column, the returned payload — sees ONLY
that normalised form. :func:`_compute_receipt_hash` does NOT re-normalise;
it hashes what it is given.

That single-normalisation point is load-bearing, not tidiness. A prior
revision upper-cased ``po_ref`` for the hash but stored it VERBATIM, so
idempotency was case-insensitive while the stored column and
``idx_goods_receipts_namespace_po`` were case-sensitive: submitting
``po-1001`` and then ``PO-1001`` was correctly detected as a replay, yet
``SELECT … WHERE po_ref = 'PO-1001'`` found nothing. Batch 133's matcher
queries that exact column, so the divergence would have leaked into the next
wave. The chosen normal form is UPPER-CASED-AND-STRIPPED, documented on the
column itself in migration 052.

Two genuinely distinct deliveries: what ``delivery_note_ref`` fixes
------------------------------------------------------------------------
Two 5-unit shipments against ONE PO line of 10, to the same location, with
no scans, produce a byte-identical line set — so, on the hash inputs above
MINUS ``delivery_note_ref``, an identical ``receipt_hash``: the second
GENUINE delivery would be swallowed as a replay and 5 units of stock
silently lost. Partial deliveries against one PO are ROUTINE in procurement,
and the ``scans`` escape hatch only helps when serials happen to be
captured, which is explicitly optional. Documenting that as an acceptable
trade was not good enough.

``delivery_note_ref`` — the delivery-note / packing-slip number that arrives
on the paperwork with the pallet — is therefore an OPTIONAL caller-supplied
part of the hash. Two genuine partial deliveries carry two different note
numbers and hash differently; a true retry of the SAME note keeps the same
hash and stays idempotent.

**When it is omitted, behaviour is exactly as it was — including the
idempotency key's own VALUES.** ``None`` OMITS the ``"delivery_note_ref"``
key from the hash payload entirely rather than hashing it as ``null``, so a
note-less receipt produces the byte-identical digest it produced before this
parameter existed. Two identical line sets against the same PO/location
still collide into ONE receipt; the collision is not removed by this
parameter — it is made AVOIDABLE by a caller that has a delivery note to
supply, which any real goods-receipt flow does.

That omission is the difference between a compatible change and a silent
upgrade hazard, and it is the ONE property the three collision properties
above do not cover. An earlier revision of this wave always emitted
``"delivery_note_ref": null`` and thereby changed the digest of every
receipt that omits a note: the same delivery recorded before the upgrade and
retried after it hashes differently, ``goods_receipts_idempotency_uq`` does
not recognise the retry as a replay, and the receipt is re-applied —
DOUBLE-STOCKING ``qty_on_hand`` for one physical delivery. The claim
"behaviour is exactly as it was" is now literally true rather than true only
of the collision properties, and
``tests/test_inventory_gr.py::test_a_note_less_receipt_hashes_exactly_as_it_did_before_delivery_note_ref_existed``
pins the pre-change digest as a literal so no future payload-shape change
can reintroduce the class.

Explicit out of scope — by name, with the batch that owns it
--------------------------------------------------------------
Nothing below is a silent omission; each is named so a reader never has to
wonder whether it was forgotten:

  * **The graph projection** — a ``GOODS_RECEIPT`` kg_node and its
    ``-[against]->PO`` / ``-[of]->SKU`` edges — is **Batch 132b**'s. This
    module writes NO ``kg_nodes`` and NO ``kg_edges`` at all, and imports
    nothing from ``nce.entity_resolution.ownership`` or
    ``nce.events.emit``.
  * **The serial→Assets hand-off** is **Batch 132b**'s. Assets (Module 9)
    does not exist yet; this module persists ``scans`` (including serials)
    onto the receipt row as the RECORD, but creates no A2A grant and imports
    nothing from ``nce.a2a``.
  * **The C4 ``GOODS_RECEIPT.created`` publish** is **Batch 132c**'s, and
    132c is currently BLOCKED (``register_automation_subscribers()`` has
    zero production callers on main, so a publish today would look green and
    deliver nothing). This module never calls ``nce.events.bus.publish``.
  * **Procurement's ``procurement_evaluate_match`` fire** and the
    **``match_result`` column's population** are **Batch 133**'s. This
    module creates the column (migration 052) and never writes to it; it
    imports nothing from ``nce.vertical_modules.procurement``.
  * **The ``BOM_LINE`` → ``DELIVERED`` status flip** is **Batch 133b**'s.
    This module never reads or writes ``BOM_LINE``.
  * **MCP tool registration and REST routes** are **Batch 138a**'s.
    :func:`do_record_goods_receipt` is unreachable from any surface when
    this wave lands — nothing in ``nce/tool_registry.py`` or
    ``nce/admin_app.py`` references it.

Dependency direction (uncle-bob-craft)
-----------------------------------------
This module imports only ``asyncpg``, ``nce.db_utils.scoped_pg_session``, and
its sibling ``transactions.py``'s typed reason constant + ``append_transaction``
(imported as a bare module-level name — see the docstring of
:func:`do_record_goods_receipt` for why that specific import form is
load-bearing for this wave's own mutation-testing proof) — no web/HTTP/admin
framework imports. ``NCEEngine`` is imported under ``TYPE_CHECKING`` only,
matching ``stock.py``'s and ``transactions.py``'s own convention.

Decimal coercion is duplicated from ``stock.py``/``transactions.py``, not
imported
------------------------------------------------------------------------------
Same reasoning as ``transactions.py``'s own docstring: the coercion helpers
in both sibling modules are module-private (leading underscore), so this
module carries its own small, adapted copies rather than reaching across a
module boundary for a private name. ``Decimal(str(x))``, never ``Decimal(x)``
— quantities to 3dp (``inventory_items.qty_on_hand``'s ``NUMERIC(18,3)``),
costs to 2dp (``inventory_transactions.unit_cost``'s ``NUMERIC(18,2)``),
BEFORE binding to any query.
"""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.inventory.transactions import (
    REASON_GOODS_RECEIPT,
    append_transaction,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.goods_receipt")

# ---------------------------------------------------------------------------
# Decimal coercion — Decimal end-to-end, quantised BEFORE binding to any
# query. Duplicated from stock.py/transactions.py (module-private there),
# same discipline (see module docstring).
# ---------------------------------------------------------------------------

_QTY_SCALE: Decimal = Decimal("0.001")
_ZERO_QTY: Decimal = Decimal("0.000")
_COST_SCALE: Decimal = Decimal("0.01")


def _as_decimal(value: Any, where: str) -> Decimal:
    """Coerce a caller-supplied number to an exact, finite ``Decimal``.

    ``bool`` is rejected before the ``int`` branch (``isinstance(True, int)``
    is ``True`` in Python); a float is converted via ``Decimal(str(x))``,
    never ``Decimal(x)`` — the latter imports the binary-float representation
    error."""
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
    """Round to inventory_items.qty_on_hand's own column scale (3dp), ties
    away from zero."""
    try:
        return value.quantize(_QTY_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: quantity is too large to express to 3dp: {value!r}") from exc


def _quantise_cost(value: Decimal, where: str) -> Decimal:
    """Round to inventory_transactions.unit_cost's own column scale (2dp),
    ties away from zero."""
    try:
        return value.quantize(_COST_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: cost is too large to express to 2dp: {value!r}") from exc


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _as_location_uuid(raw: Any, where: str) -> UUID:
    if not raw:
        raise ValueError(f"{where}: a location id is required")
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise ValueError(f"{where}: expected a UUID string, got {raw!r}") from exc


def _as_po_ref(raw: Any) -> str:
    """THE single normalisation point for ``po_ref`` — stripped, upper-cased.

    Everything downstream (the receipt hash, the stored ``po_ref`` column,
    the returned payload) uses this return value and re-normalises nothing.
    See the module docstring's "Text refs are normalised ONCE" section for
    the case-sensitivity divergence this closes."""
    po_ref = str(raw or "").strip().upper()
    if not po_ref:
        raise ValueError("do_record_goods_receipt: 'po_ref' is required and must be non-empty")
    return po_ref


def _as_optional_delivery_note_ref(raw: Any) -> str | None:
    """THE single normalisation point for ``delivery_note_ref`` — optional,
    stripped, upper-cased, blank collapsed to ``None``.

    ``None`` (absent, or whitespace-only) is a legal value and means "this
    caller has no delivery note to distinguish deliveries by": the receipt
    hash then behaves exactly as it did before this parameter existed. See
    the module docstring's "Two genuinely distinct deliveries" section."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            "do_record_goods_receipt: 'delivery_note_ref' must be a string or None, "
            f"got {type(raw).__name__} {raw!r}"
        )
    normalised = raw.strip().upper()
    return normalised or None


def _as_qty(raw: Any, where: str) -> Decimal:
    """Required, strictly-positive quantity, quantised to 3dp."""
    if raw is None:
        raise ValueError(f"{where}: a quantity is required, got None")
    quantised = _quantise_qty(_as_decimal(raw, where), where)
    if quantised <= _ZERO_QTY:
        raise ValueError(f"{where}: qty must be > 0, got {quantised}")
    return quantised


def _as_optional_unit_cost(raw: Any, where: str) -> Decimal | None:
    """Optional per-line unit cost, quantised to 2dp. ``None`` is a legal,
    honest value — not every line's cost is known at receipt time."""
    if raw is None:
        return None
    return _quantise_cost(_as_decimal(raw, where), where)


# ---------------------------------------------------------------------------
# Validation + normalisation — pure, no I/O (one job: reject malformed input
# and produce the canonical, aggregated, sku-sorted shape everything else
# operates on).
# ---------------------------------------------------------------------------


def _validate_and_aggregate_lines(raw_lines: Any) -> list[tuple[str, Decimal, Decimal | None]]:
    """Validate ``lines[]``, aggregate duplicate SKUs, and return them in
    ASCENDING SKU ORDER.

    ``unit_cost`` agreement is decided on the set of DISTINCT NON-``None``
    costs gathered for a sku, and only after every entry has been read — so
    the verdict is a function of the line SET, never of the array ORDER.
    ``None`` means "not captured on this entry", NEVER "disagrees": one PO
    line split across two pallets with the cost written on only one of them
    is routine, and an order-sensitive verdict there broke idempotent retry
    outright (the first submission succeeded; the retry, re-serialised in the
    other order, raised instead of reporting the replay). Only TWO OR MORE
    distinct non-``None`` costs are genuinely ambiguous, and that raises
    ``ValueError`` naming the sku and every conflicting value rather than
    silently picking one.

    Returns
    -------
    list of ``(sku, total_qty, unit_cost)`` sorted by ``sku`` — the
    deterministic order :func:`do_record_goods_receipt` processes lines in,
    so two concurrent receipts sharing SKUs can never form a lock-order
    cycle (module docstring's "Deterministic per-line ordering").
    """
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValueError("do_record_goods_receipt: 'lines' must be a non-empty list")

    totals: dict[str, Decimal] = {}
    # Distinct non-None costs per sku, collected across ALL entries before any
    # verdict is reached. A list (not a set) keeps the report deterministic
    # after the sort below; membership is checked by value.
    distinct_costs: dict[str, list[Decimal]] = {}
    for idx, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            raise ValueError(f"do_record_goods_receipt: lines[{idx}] must be an object")
        sku = str(line.get("sku") or "").strip()
        if not sku:
            raise ValueError(f"do_record_goods_receipt: lines[{idx}].sku must be non-empty")
        qty = _as_qty(line.get("qty"), f"do_record_goods_receipt: lines[{idx}].qty")
        unit_cost = _as_optional_unit_cost(
            line.get("unit_cost"), f"do_record_goods_receipt: lines[{idx}].unit_cost"
        )

        if sku in totals:
            totals[sku] = totals[sku] + qty
        else:
            totals[sku] = qty
            distinct_costs[sku] = []
        if unit_cost is not None and unit_cost not in distinct_costs[sku]:
            distinct_costs[sku].append(unit_cost)

    for sku in sorted(distinct_costs):
        captured = distinct_costs[sku]
        if len(captured) > 1:
            conflicting = ", ".join(str(cost) for cost in sorted(captured))
            raise ValueError(
                f"do_record_goods_receipt: sku {sku!r} appears with disagreeing "
                f"unit_cost values ({conflicting}) — ambiguous, refusing to guess "
                "which is correct"
            )

    return [
        (sku, totals[sku], distinct_costs[sku][0] if distinct_costs[sku] else None)
        for sku in sorted(totals)
    ]


def _validate_scans(raw_scans: Any) -> list[dict[str, str | None]]:
    """Validate ``scans[]`` — optional per-unit barcode/serial capture.

    Normalised into a deterministic shape (fixed key order via the dict
    literal, values stripped) so :func:`_compute_receipt_hash` is stable
    across equivalent inputs (e.g. trailing whitespace in a serial)."""
    if raw_scans is None:
        return []
    if not isinstance(raw_scans, list):
        raise ValueError("do_record_goods_receipt: 'scans' must be a list when given")

    normalised: list[dict[str, str | None]] = []
    for idx, scan in enumerate(raw_scans):
        if not isinstance(scan, dict):
            raise ValueError(f"do_record_goods_receipt: scans[{idx}] must be an object")
        sku = str(scan.get("sku") or "").strip()
        if not sku:
            raise ValueError(f"do_record_goods_receipt: scans[{idx}].sku must be non-empty")
        serial = scan.get("serial")
        if serial is not None and (not isinstance(serial, str) or not serial.strip()):
            raise ValueError(
                f"do_record_goods_receipt: scans[{idx}].serial must be a non-empty string or None"
            )
        barcode = scan.get("barcode")
        if barcode is not None and (not isinstance(barcode, str) or not barcode.strip()):
            raise ValueError(
                f"do_record_goods_receipt: scans[{idx}].barcode must be a non-empty string or None"
            )
        normalised.append(
            {
                "sku": sku,
                "serial": serial.strip() if isinstance(serial, str) else None,
                "barcode": barcode.strip() if isinstance(barcode, str) else None,
            }
        )
    # Deterministic order — sku, then serial, then barcode (None sorts before
    # any string as "" for ordering purposes only; the stored value keeps None).
    normalised.sort(key=lambda s: (s["sku"], s["serial"] or "", s["barcode"] or ""))
    return normalised


# ---------------------------------------------------------------------------
# Idempotency key — sha256 over a canonically normalised encoding. Hash
# stability is load-bearing: a different encoding of the same delivery is a
# different hash and double-stocks (module docstring's "Hash stability").
# ---------------------------------------------------------------------------


def _compute_receipt_hash(
    po_ref: str,
    location_id: UUID,
    lines: list[tuple[str, Decimal, Decimal | None]],
    scans: list[dict[str, str | None]],
    *,
    delivery_note_ref: str | None,
) -> str:
    """sha256 hex digest over ``(po_ref, location_id, lines, scans)``, plus
    ``delivery_note_ref`` ONLY when one was supplied (see "An absent delivery
    note OMITS THE KEY" below).

    *po_ref* and *delivery_note_ref* must ALREADY be normalised — this
    function hashes them verbatim and deliberately does NOT strip or
    upper-case them itself. :func:`_as_po_ref` and
    :func:`_as_optional_delivery_note_ref` are the single normalisation
    point; normalising a second time here is what let the hash and the
    stored column disagree (module docstring's "Text refs are normalised
    ONCE"). *delivery_note_ref* is keyword-only and has NO default, so no
    call site can silently omit it and hash a different payload.

    ``received_at``/``created_at``/``id`` are deliberately NOT part of this —
    including a timestamp would make every retry a new receipt and defeat
    idempotency entirely (module docstring). Quantities and costs are
    rendered as their QUANTISED DECIMAL STRINGS, never floats — two
    representations of the same decimal value (e.g. ``Decimal("5.00")`` vs a
    hypothetical ``5.0``) must hash identically, which only holds if both are
    forced through the same quantised ``str()``.

    An absent delivery note OMITS THE KEY — it does not hash as ``null``
    ------------------------------------------------------------------------
    When *delivery_note_ref* is ``None`` the ``"delivery_note_ref"`` key is
    left OUT of the payload entirely, so a note-less receipt hashes to the
    byte-identical digest it did before this parameter existed. Emitting
    ``"delivery_note_ref": null`` instead would be a silent UPGRADE HAZARD,
    not a cosmetic difference: every goods receipt already recorded without a
    note would keep its OLD digest in ``goods_receipts.receipt_hash`` while
    the same delivery resubmitted after the upgrade computes a NEW one, the
    ``goods_receipts_idempotency_uq`` index would not recognise the retry as
    a replay, and the receipt would be re-applied — a second receipt row, a
    second ledger row, and ``qty_on_hand`` incremented TWICE for one physical
    delivery. Omitting the key is what makes "when the note is omitted,
    behaviour is exactly as it was" literally true of the idempotency key
    itself and not merely of its collision properties.
    """
    payload: dict[str, Any] = {
        "po_ref": po_ref,
        "location_id": str(location_id),
        "lines": [
            {
                "sku": sku,
                "qty": str(qty),
                "unit_cost": str(unit_cost) if unit_cost is not None else None,
            }
            for sku, qty, unit_cost in lines
        ],
        "scans": scans,
    }
    if delivery_note_ref is not None:
        # Present ONLY when supplied — see "An absent delivery note OMITS THE
        # KEY" above. ``sort_keys=True`` below makes insertion order
        # irrelevant, so adding it here hashes identically to declaring it in
        # the literal.
        payload["delivery_note_ref"] = delivery_note_ref
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# DB-touching: the atomic increment primitive. A DELIBERATE DUPLICATE of
# stock.py's _increment_on_hand — see the module docstring's "``stock.py`` is
# duplicated" section, including the withdrawn first justification.
# ---------------------------------------------------------------------------


async def _increment_qty_on_hand(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    sku: str,
    location_id: UUID,
    qty: Decimal,
) -> Decimal:
    """Atomically increment ``qty_on_hand`` by *qty* at one (sku, location),
    creating the row (at ``qty_on_hand = qty``) if it does not exist yet.

    ONE upsert statement — no read-then-write. Returns the POST-increment
    ``qty_on_hand``.

    No ``ForeignKeyViolationError`` handling, deliberately
    ---------------------------------------------------------
    ``inventory_items_location_fk`` and ``goods_receipts_location_fk`` both
    reference ``stock_locations (id, namespace_id)``, and
    :func:`do_record_goods_receipt` inserts the RECEIPT — with the identical
    ``(location_id, namespace_id)`` pair — before it ever calls this. A
    ``location_id`` that would trip this statement's FK has therefore already
    been refused, with a message, at the receipt INSERT. An earlier revision
    carried a ``ForeignKeyViolationError`` translation here that no input
    could reach; it has been removed rather than left as coverage theatre.
    ``NumericValueOutOfRangeError`` from this statement (a per-line quantity
    that does not fit ``NUMERIC(18,3)``, or a running total that no longer
    does) is translated by the caller, which knows the line it belongs to.
    """
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

    assert row is not None  # RETURNING on INSERT ... DO UPDATE always yields a row
    return row["qty_on_hand"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Public: do_record_goods_receipt
# ---------------------------------------------------------------------------


async def do_record_goods_receipt(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Record one inbound delivery: idempotent capture, authoritative stock
    increment, costed ledger append — all in ONE transaction.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "po_ref":       str,           # required, non-empty after strip
            "location_id":  str | UUID,    # required, a stock_locations id in this namespace
            "delivery_note_ref": str | None,  # OPTIONAL — see below
            "lines": [ { "sku": str, "qty": int|float|Decimal,   # required, qty > 0
                         "unit_cost": int|float|Decimal|None } ],# optional per line
            "scans": [ { "sku": str, "serial": str|None, "barcode": str|None } ],  # optional
        }``

    ``delivery_note_ref`` is the delivery-note / packing-slip number on the
    paperwork that arrives with the goods. It PARTICIPATES IN the idempotency
    hash, so two genuine PARTIAL deliveries against one PO — same location,
    byte-identical line set, no scans — are two receipts rather than one
    swallowed replay, while a true retry of the SAME note stays idempotent.
    It is OPTIONAL: omitted (or blank), the key is left OUT of the hash
    payload entirely — NOT hashed as ``null`` — so the receipt's digest is
    byte-identical to the one it had before this parameter existed, and
    behaviour is EXACTLY what it was, including the collision above. Hashing
    an absent note as ``null`` instead would silently re-key every
    already-recorded note-less receipt and make its retry double-stock. See
    the module docstring's "Two genuinely distinct deliveries" section and
    :func:`_compute_receipt_hash`.

    ``po_ref`` and ``delivery_note_ref`` are stripped and upper-cased once,
    at the boundary, and that normalised form is what is hashed AND what is
    stored — so a query against ``goods_receipts.po_ref`` always agrees with
    what idempotency considered the same receipt.

    Idempotency (module docstring): the receipt's own INSERT uses
    ``ON CONFLICT (namespace_id, receipt_hash) DO NOTHING``. When it returns
    no row, this call IS a replay — it writes NOTHING else (no increment, no
    ledger row) and returns ``{"ok": True, "duplicate": True, "receipt_id":
    <existing id>}``. This gating is never re-expressed as a
    ``SELECT ... THEN INSERT`` pre-check, which would reintroduce the exact
    race the unique index exists to close.

    Lines are processed in ASCENDING SKU ORDER (after aggregating duplicate
    SKUs within this call) — deterministic, so two concurrent receipts
    sharing an overlapping SKU set at the same location cannot form a
    lock-order cycle (module docstring's "Deterministic per-line ordering").

    For each line, immediately after its stock increment, this appends one
    ``inventory_transactions`` row via ``transactions.append_transaction`` —
    called as a bare module-level name (``from ...transactions import
    append_transaction``), not as ``transactions.append_transaction`` — on
    the SAME ``conn``, inside the SAME transaction as the receipt row and the
    increment. That is what makes the receipt row, the stock increment, and
    the ledger row commit or roll back TOGETHER, by construction. ``ref`` on
    the ledger row is the receipt's own id, so a valuation lot can be walked
    back to the delivery that created it.

    Returns
    -------
    dict
        ``{"ok": True, "duplicate": False, "receipt_id", "po_ref",
        "delivery_note_ref", "location_id",
        "lines": [{"sku", "qty", "unit_cost", "on_hand"}],
        "ledger_rows": <int>}`` on a fresh receipt, or
        ``{"ok": True, "duplicate": True, "receipt_id"}`` on a replay.

    Raises
    ------
    ValueError
        The ONLY exception type this function raises. Concretely:

          * Any required field missing/malformed — empty ``po_ref``,
            non-string ``delivery_note_ref``, empty/absent ``lines``,
            non-positive or unparsable ``qty``, malformed ``scans``, or two
            or more DISTINCT non-``None`` ``unit_cost`` values for one sku.
          * ``namespace_id`` is not a ``namespaces`` row
            (``goods_receipts_namespace_id_fkey``), or ``location_id`` is not
            a ``stock_locations`` row in this namespace (the composite
            ``goods_receipts_location_fk``) — the receipt INSERT's two
            independent foreign keys are distinguished by
            ``exc.constraint_name``, never collapsed into one message (a
            prior version of this function blamed ``location_id`` for both).
          * A quantity or cost that does not fit its ``NUMERIC`` column —
            either the line's own value, or the on-hand total it would
            produce. Postgres raises ``asyncpg.NumericValueOutOfRangeError``
            for that, and the per-line loop below TRANSLATES it, naming the
            sku. Without that translation the raw driver exception escaped
            and this documented contract was simply false:
            :func:`_quantise_qty`'s own "too large" ``ValueError`` guards
            Decimal's 28-digit context precision, which at 3dp first trips at
            10^25 — TEN ORDERS OF MAGNITUDE beyond the ``NUMERIC(18,3)``
            column's own 10^15 ceiling (measured, not assumed), so it never
            fires first.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    po_ref = _as_po_ref(params.get("po_ref"))
    delivery_note_ref = _as_optional_delivery_note_ref(params.get("delivery_note_ref"))
    location_id = _as_location_uuid(params.get("location_id"), "location_id")
    lines = _validate_and_aggregate_lines(params.get("lines"))
    scans = _validate_scans(params.get("scans"))

    receipt_hash = _compute_receipt_hash(
        po_ref, location_id, lines, scans, delivery_note_ref=delivery_note_ref
    )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        try:
            inserted = await conn.fetchrow(
                """
                INSERT INTO goods_receipts
                    (namespace_id, po_ref, delivery_note_ref, location_id,
                     lines, scans, receipt_hash)
                VALUES ($1::uuid, $2, $3, $4::uuid, $5::jsonb, $6::jsonb, $7)
                ON CONFLICT (namespace_id, receipt_hash) DO NOTHING
                RETURNING id
                """,
                str(ns_uuid),
                po_ref,
                delivery_note_ref,
                str(location_id),
                json.dumps(
                    [
                        {
                            "sku": sku,
                            "qty": str(qty),
                            "unit_cost": str(uc) if uc is not None else None,
                        }
                        for sku, qty, uc in lines
                    ]
                ),
                json.dumps(scans),
                receipt_hash,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            # goods_receipts carries TWO independent foreign keys — the
            # composite goods_receipts_location_fk (location_id, namespace_id)
            # -> stock_locations, and the plain, Postgres-auto-named
            # goods_receipts_namespace_id_fkey (namespace_id) -> namespaces.
            # Either can fire on this one INSERT, and they mean DIFFERENT
            # things (a bad location vs. a bad tenant) — branch on
            # exc.constraint_name rather than assuming which one it was.
            # A prior version of this function collapsed both into the
            # location message, which actively mis-blamed a valid location
            # when the real problem was an invalid namespace_id.
            constraint = exc.constraint_name
            if constraint == "goods_receipts_location_fk":
                raise ValueError(
                    f"do_record_goods_receipt: location_id {location_id} does not exist "
                    "in this namespace"
                ) from exc
            if constraint == "goods_receipts_namespace_id_fkey":
                raise ValueError(
                    f"do_record_goods_receipt: namespace_id {ns_uuid} does not exist"
                ) from exc
            # Neither known name matched (e.g. a future schema change renamed
            # a constraint) — say so explicitly rather than guessing which
            # field is at fault.
            raise ValueError(
                f"do_record_goods_receipt: foreign key violation on receipt insert "
                f"(constraint={constraint!r}); namespace_id={ns_uuid} "
                f"location_id={location_id}"
            ) from exc

        if inserted is None:
            # Replay: an identical receipt already exists. THE gate — every
            # side effect below is skipped entirely, never conditionally
            # undone after the fact.
            existing = await conn.fetchrow(
                """
                SELECT id FROM goods_receipts
                WHERE namespace_id = $1::uuid AND receipt_hash = $2
                """,
                str(ns_uuid),
                receipt_hash,
            )
            assert existing is not None  # the unique index guarantees this row exists
            log.info(
                "do_record_goods_receipt: replay ns=%s po_ref=%s receipt_id=%s",
                ns_uuid,
                po_ref,
                existing["id"],
            )
            return {"ok": True, "duplicate": True, "receipt_id": str(existing["id"])}

        receipt_id: UUID = inserted["id"]

        result_lines: list[dict[str, Any]] = []
        ledger_rows = 0
        for sku, qty, unit_cost in lines:
            # NumericValueOutOfRangeError can come from EITHER statement below
            # — inventory_items.qty_on_hand and inventory_transactions.delta
            # are NUMERIC(18,3), inventory_transactions.unit_cost is
            # NUMERIC(18,2) — so the translation wraps both rather than
            # sitting inside one of them. It is caught HERE because this is
            # the only frame that knows which line it belongs to.
            try:
                on_hand = await _increment_qty_on_hand(conn, ns_uuid, sku, location_id, qty)
                await append_transaction(
                    conn,
                    ns_uuid,
                    sku=sku,
                    location_id=location_id,
                    delta=qty,
                    reason_category=REASON_GOODS_RECEIPT,
                    ref=str(receipt_id),
                    unit_cost=unit_cost,
                )
            except asyncpg.NumericValueOutOfRangeError as exc:
                raise ValueError(
                    f"do_record_goods_receipt: sku {sku!r} does not fit the inventory "
                    f"numeric columns (qty={qty}, unit_cost={unit_cost}) — qty_on_hand "
                    "and delta are NUMERIC(18,3) (max 999999999999999.999) and unit_cost "
                    "is NUMERIC(18,2) (max 9999999999999999.99). Either this line's own "
                    "value is too large, or adding it to the existing on-hand quantity "
                    "would overflow the column"
                ) from exc
            ledger_rows += 1
            result_lines.append(
                {
                    "sku": sku,
                    "qty": qty,
                    "unit_cost": unit_cost,
                    "on_hand": on_hand,
                }
            )

    log.info(
        "do_record_goods_receipt: ns=%s po_ref=%s delivery_note_ref=%s receipt_id=%s "
        "location=%s lines=%d",
        ns_uuid,
        po_ref,
        delivery_note_ref,
        receipt_id,
        location_id,
        len(result_lines),
    )
    return {
        "ok": True,
        "duplicate": False,
        "receipt_id": str(receipt_id),
        "po_ref": po_ref,
        "delivery_note_ref": delivery_note_ref,
        "location_id": str(location_id),
        "lines": result_lines,
        "ledger_rows": ledger_rows,
    }
