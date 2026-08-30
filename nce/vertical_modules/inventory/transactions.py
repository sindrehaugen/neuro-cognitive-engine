"""
nce/vertical_modules/inventory/transactions.py
================================================
The append-only inventory movement ledger + FIFO/average valuation
(Module 11, Wave 11 — ``transactions-valuation``), migration 051's
``inventory_transactions`` table.

Re-sequenced ahead of its numeric slot (runs right after Batch 130, before
Batch 131) per this wave's own orchestrator amendment: Batch 134 needs a
ledger-backed consumption rationale and ``inventory_items`` (migration 050)
carries only a current quantity, no history; Batch 131 exposes
``do_transfer_stock``/``do_record_consumption`` as MCP tools and must not do
so before every movement is recorded.

Two responsibilities, one module
---------------------------------
1. :func:`append_transaction` — the SOLE writer of ``inventory_transactions``
   rows. Called by ``stock.py``'s ``do_transfer_stock`` /
   ``do_record_consumption`` **inside their existing ``scoped_pg_session``
   transaction**, so a movement's row write and its ledger row commit or roll
   back together, by construction, never by convention. It is never given its
   own connection or transaction — see ``stock.py``'s call sites.
2. :func:`do_valuation` — a read-only FIFO/average-cost computation over one
   (sku, location)'s ledger rows, per ``nce/config_data/
   inventory-valuation.json``. **Inventory VALUES the stock; Economy POSTS
   it** — this module imports nothing from ``nce.vertical_modules.economy``,
   never upserts a ``POSTING`` kg_node, and never writes ``economy_postings``.
   The boundary is enforced structurally (there is nothing to post *to* in
   this file's import graph), not by a comment alone.

Honest scope limit — do not overclaim
---------------------------------------
``inventory_items`` has no cost column (migration 050's own docstring says
so), so a unit cost only exists where THIS ledger row carries one. Costed
inbound is now real (Batch 132, migration 052): ``goods_receipt`` rows carry
a real ``unit_cost`` supplied by
``nce/vertical_modules/inventory/goods_receipt.py``'s
``do_record_goods_receipt``, appended via :func:`append_transaction` inside
that function's own transaction. ``stock.py``'s ``transfer_in`` /
``transfer_out`` / ``consumption`` writers still legitimately carry
``unit_cost = NULL`` — no cost source exists at a transfer or a consumption,
only at the inbound receipt that first brought the stock in. An inbound row
recorded WITHOUT a cost (every ``transfer_in`` this module's callers write)
still opens a FIFO layer, valued at **zero** — never skipped — so the
layered quantity always matches the ledger's own arithmetic exactly; only the
VALUE of stock with an unknown cost is understated, never its quantity.

Typed reason categories, not free text
-----------------------------------------
Rackbeat's movements-vs-adjustments split (research doc A5): every qty
change is an append-only transaction with a typed reason, enforced by
migration 051's ``CHECK (reason_category IN (...))`` (widened by migration
052, Batch 132, to admit ``goods_receipt``) AND re-checked here
(:func:`_assert_sign_matches_category`) for a clearer domain error before the
row ever reaches Postgres. Five categories, each with a real writer or test —
no speculative category is added ahead of a writer that would use it:

* ``transfer_in`` / ``transfer_out`` — ``do_transfer_stock``'s two sides.
* ``consumption`` — ``do_record_consumption``.
* ``adjustment`` — the one open-signed category, for a manual/typed
  correction; this wave's own valuation tests use it to seed a cost-bearing
  inbound row standing in for a not-yet-built goods receipt.
* ``goods_receipt`` (Batch 132) — ``goods_receipt.py``'s
  ``do_record_goods_receipt``, the first writer of a REAL costed inbound row
  (``unit_cost`` supplied by the caller, not seeded). Positive-delta-only,
  like ``transfer_in``.

Dependency direction (uncle-bob-craft)
-----------------------------------------
This module imports only ``asyncpg`` and ``nce.db_utils.scoped_pg_session`` —
no web/HTTP/admin framework imports, and nothing from
``nce.vertical_modules.economy`` (the GL-posting boundary above).
``NCEEngine`` is imported under ``TYPE_CHECKING`` only, matching
``stock.py``'s own convention.

Decimal coercion is duplicated from ``stock.py``, not imported
------------------------------------------------------------------
``stock.py``'s ``_as_quantity``/``_quantise_qty`` are module-private
(leading underscore) and this wave does not touch that file beyond appending
ledger calls inside its existing transaction (see ``stock.py``'s own
docstring on why it is dangerous to refactor). This module therefore carries
its own small, adapted copies (:func:`_as_decimal`, :func:`_quantise_qty`,
:func:`_quantise_cost`) — the same duplication-over-cross-module-private-
import choice ``economy``'s ``contracts.py``/``ngaap.py``/``recurring.py``
already make for their own ``_quantise``.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.transactions")

# ---------------------------------------------------------------------------
# Typed reason categories — migration 051's CHECK's Python-side mirror,
# widened by migration 052 (Batch 132 goods-receipt) to add REASON_GOODS_RECEIPT.
# ---------------------------------------------------------------------------

REASON_TRANSFER_IN = "transfer_in"
REASON_TRANSFER_OUT = "transfer_out"
REASON_CONSUMPTION = "consumption"
REASON_ADJUSTMENT = "adjustment"
REASON_GOODS_RECEIPT = "goods_receipt"

_VALID_REASON_CATEGORIES: frozenset[str] = frozenset(
    {
        REASON_TRANSFER_IN,
        REASON_TRANSFER_OUT,
        REASON_CONSUMPTION,
        REASON_ADJUSTMENT,
        REASON_GOODS_RECEIPT,
    }
)

# Engine-authored write, not an external-system sync — mirrors stock.py's own
# 'agent' choice (never 'sync', which is reserved for the D365 external-sync
# origin).
_DEFAULT_CHANGE_ORIGIN = "agent"

# ---------------------------------------------------------------------------
# Decimal coercion — Decimal end-to-end, quantised BEFORE binding to any
# query (see module docstring's "Decimal coercion is duplicated" section).
# ---------------------------------------------------------------------------

_QTY_SCALE: Decimal = Decimal("0.001")
_ZERO_QTY: Decimal = Decimal("0.000")
_COST_SCALE: Decimal = Decimal("0.01")
_ZERO_COST: Decimal = Decimal("0.00")

_VALID_METHODS: frozenset[str] = frozenset({"fifo", "average"})


def _as_decimal(value: Any, where: str) -> Decimal:
    """Coerce a caller-supplied number to an exact, finite ``Decimal``.

    ``bool`` is rejected before the ``int`` branch (``isinstance(True, int)``
    is ``True`` in Python); a float is converted via ``Decimal(str(x))``,
    never ``Decimal(x)`` — the latter imports the binary-float representation
    error (stock.py's "Quantity precision" section argues this at length)."""
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
    """Round to inventory_transactions.delta's own column scale (3dp),
    ties away from zero — same scale and rounding as
    ``inventory_items.qty_on_hand`` (stock.py's ``_quantise_qty``)."""
    try:
        return value.quantize(_QTY_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: quantity is too large to express to 3dp: {value!r}") from exc


def _quantise_cost(value: Decimal, where: str) -> Decimal:
    """Round to inventory_transactions.unit_cost's own column scale (2dp),
    ties away from zero — same discipline as economy_postings.amount."""
    try:
        return value.quantize(_COST_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: cost is too large to express to 2dp: {value!r}") from exc


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


def _assert_sign_matches_category(reason_category: str, delta: Decimal) -> None:
    """Python-side mirror of migration 051's (widened by migration 052)
    ``inventory_transactions_sign_matches_category`` CHECK — a clearer domain
    error than a raw ``asyncpg.CheckViolationError`` for the same mistake.
    ``adjustment`` is deliberately unconstrained: a manual correction can
    move quantity either way. ``goods_receipt`` requires a positive delta,
    same as ``transfer_in``."""
    if reason_category in (REASON_TRANSFER_IN, REASON_GOODS_RECEIPT) and delta <= _ZERO_QTY:
        raise ValueError(
            f"append_transaction: {reason_category!r} requires a positive delta, got {delta}"
        )
    if reason_category in (REASON_TRANSFER_OUT, REASON_CONSUMPTION) and delta >= _ZERO_QTY:
        raise ValueError(
            f"append_transaction: {reason_category!r} requires a negative delta, got {delta}"
        )


# ---------------------------------------------------------------------------
# The sole writer — append_transaction. MUST be called with a *conn* already
# inside an open scoped_pg_session transaction (see module docstring).
# ---------------------------------------------------------------------------


async def append_transaction(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    *,
    sku: str,
    location_id: UUID,
    delta: Any,
    reason_category: str,
    ref: str | None = None,
    unit_cost: Any = None,
    change_origin: str = _DEFAULT_CHANGE_ORIGIN,
) -> UUID:
    """Append one immutable ``inventory_transactions`` row on *conn*.

    *conn* must already be inside an open ``scoped_pg_session`` transaction —
    this function never opens its own. That is what makes the ledger row
    commit or roll back together with the caller's ``inventory_items`` write;
    see ``stock.py``'s ``do_transfer_stock``/``do_record_consumption`` for the
    reference call sites, both of which call this in the middle of their own
    already-open transaction, never after it.

    Parameters
    ----------
    delta:
        Signed quantity change (int/float/Decimal), quantised to 3dp and
        validated non-zero and sign-consistent with *reason_category*.
    unit_cost:
        Optional signed cost per unit (int/float/Decimal), quantised to 2dp.
        ``None`` when no cost source exists for this movement (the honest
        default for ``stock.py``'s transfer/consumption writers this wave).

    Returns
    -------
    UUID
        The new row's ``id``.

    Raises
    ------
    ValueError
        *reason_category* is not one of the typed categories, *delta*
        quantises to zero, or its sign disagrees with *reason_category*.
    """
    if reason_category not in _VALID_REASON_CATEGORIES:
        raise ValueError(
            f"append_transaction: reason_category must be one of "
            f"{sorted(_VALID_REASON_CATEGORIES)}, got {reason_category!r}"
        )

    quantised_delta = _quantise_qty(_as_decimal(delta, "delta"), "delta")
    if quantised_delta == _ZERO_QTY:
        raise ValueError("append_transaction: delta must be non-zero")
    _assert_sign_matches_category(reason_category, quantised_delta)

    quantised_cost = (
        _quantise_cost(_as_decimal(unit_cost, "unit_cost"), "unit_cost")
        if unit_cost is not None
        else None
    )

    row = await conn.fetchrow(
        """
        INSERT INTO inventory_transactions
            (namespace_id, sku, location_id, delta, reason_category, unit_cost, ref, change_origin)
        VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        str(ns_uuid),
        sku,
        str(location_id),
        quantised_delta,
        reason_category,
        quantised_cost,
        ref,
        change_origin,
    )
    assert row is not None  # RETURNING on a plain INSERT always yields a row
    return row["id"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Valuation config loader — reads nce/config_data/inventory-valuation.json
# (no config class), mirrors economy/matching.py's load_economy_thresholds().
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"
_VALUATION_CONFIG_FILENAME = "inventory-valuation.json"


def load_inventory_valuation_config() -> dict[str, Any]:
    """Load and return the contents of ``inventory-valuation.json``.

    Returns
    -------
    dict with key ``method`` (``"fifo"`` or ``"average"``). Global — not
    namespace-scoped — for this wave (see the file's own ``_comment``).
    """
    path = _CONFIG_DATA_DIR / _VALUATION_CONFIG_FILENAME
    with path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    return config


# ---------------------------------------------------------------------------
# Pure valuation math — no DB, no asyncpg awareness. Takes plain
# {"delta": Decimal, "unit_cost": Decimal | None} rows, already ordered
# oldest-first by the caller.
# ---------------------------------------------------------------------------


class _ValuationResult(NamedTuple):
    total_value: Decimal
    remaining_qty: Decimal


def _compute_fifo(rows: Sequence[Mapping[str, Any]]) -> _ValuationResult:
    """FIFO: consume the OLDEST open layer first. An inbound row without a
    cost still opens a layer (valued at zero) — see module docstring's
    "Honest scope limit"; an outbound row consumes quantity from the front of
    the queue regardless of whether it itself carries a cost (only inbound
    rows carry one). An outbound total exceeding what is layered is clipped
    at zero rather than driven negative — defensive only; this wave's own
    writers can never produce that (inventory_items' own oversell guard
    already prevents it), but a directly-seeded test ledger could."""
    layers: deque[list[Decimal]] = deque()  # each: [qty, unit_cost]
    for row in rows:
        delta: Decimal = row["delta"]
        if delta > _ZERO_QTY:
            cost = row["unit_cost"] if row["unit_cost"] is not None else _ZERO_COST
            layers.append([delta, cost])
            continue
        to_remove = -delta
        while to_remove > _ZERO_QTY and layers:
            layer = layers[0]
            take = min(layer[0], to_remove)
            layer[0] -= take
            to_remove -= take
            if layer[0] <= _ZERO_QTY:
                layers.popleft()

    total_value = sum((layer[0] * layer[1] for layer in layers), _ZERO_COST)
    remaining_qty = sum((layer[0] for layer in layers), _ZERO_QTY)
    return _ValuationResult(total_value=total_value, remaining_qty=remaining_qty)


def _compute_average(rows: Sequence[Mapping[str, Any]]) -> _ValuationResult:
    """Weighted-average: every inbound row folds into one running
    qty/value pair; every outbound row removes quantity at the CURRENT
    average (computed just-in-time, since it never changes between two
    outbound rows with no inbound between them). Same zero-cost-layer and
    clip-at-zero conventions as :func:`_compute_fifo`."""
    running_qty = _ZERO_QTY
    running_value = _ZERO_COST
    for row in rows:
        delta: Decimal = row["delta"]
        if delta > _ZERO_QTY:
            cost = row["unit_cost"] if row["unit_cost"] is not None else _ZERO_COST
            running_value += delta * cost
            running_qty += delta
            continue
        qty_out = min(-delta, running_qty)
        if running_qty > _ZERO_QTY:
            avg_cost = running_value / running_qty
            running_value -= qty_out * avg_cost
        running_qty -= qty_out

    return _ValuationResult(total_value=running_value, remaining_qty=running_qty)


def _compute_valuation(rows: Sequence[Mapping[str, Any]], method: str) -> _ValuationResult:
    if method == "fifo":
        return _compute_fifo(rows)
    if method == "average":
        return _compute_average(rows)
    raise ValueError(f"_compute_valuation: unknown method {method!r}")


# ---------------------------------------------------------------------------
# Public: do_valuation — the read Economy consumes to post (never posts
# itself; see module docstring's boundary section).
# ---------------------------------------------------------------------------


async def do_valuation(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """FIFO/average-cost value of one (sku, location)'s stock, computed from
    ``inventory_transactions`` per ``nce/config_data/inventory-valuation.json``.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "sku":          str,          # required
            "location":     str | UUID,   # required, a stock_locations id
        }``

    Returns
    -------
    dict
        ``{"ok": True, "sku", "location_id", "method", "value",
        "remaining_qty"}`` — ``value`` is the money value of the layered
        quantity (Decimal, 2dp); ``remaining_qty`` is that layered quantity
        (Decimal, 3dp) per the module docstring's "Honest scope limit"
        (may be less than ``inventory_items.qty_on_hand`` if some of it
        arrived through an uncosted movement — never more).

    Raises
    ------
    ValueError
        Any required field missing/malformed, or
        ``inventory-valuation.json``'s ``method`` is not ``fifo``/``average``.

    This function never writes ``economy_postings`` or any GL-adjacent table
    — it only reads. Economy is the caller that turns this value into a
    posting.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    sku = _as_sku(params.get("sku"), "sku")
    location_id = _as_location_uuid(params.get("location"), "location")

    config = load_inventory_valuation_config()
    method = config.get("method")
    if method not in _VALID_METHODS:
        raise ValueError(
            f"do_valuation: inventory-valuation.json 'method' must be one of "
            f"{sorted(_VALID_METHODS)}, got {method!r}"
        )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        db_rows = await conn.fetch(
            """
            SELECT delta, unit_cost
            FROM inventory_transactions
            WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
            ORDER BY created_at ASC, id ASC
            """,
            str(ns_uuid),
            sku,
            str(location_id),
        )

    # Detach from asyncpg.Record into plain dicts — the valuation math has no
    # DB awareness at all (uncle-bob-craft: dependencies point inward).
    rows: list[dict[str, Any]] = [
        {"delta": r["delta"], "unit_cost": r["unit_cost"]} for r in db_rows
    ]
    result = _compute_valuation(rows, method)

    return {
        "ok": True,
        "sku": sku,
        "location_id": str(location_id),
        "method": method,
        "value": _quantise_cost(result.total_value, "value"),
        "remaining_qty": result.remaining_qty,
    }
