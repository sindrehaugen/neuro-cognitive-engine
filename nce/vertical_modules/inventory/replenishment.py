"""
nce/vertical_modules/inventory/replenishment.py
==================================================
Predictive replenishment Advisor (Module 11, Wave 6 -- ``restock-advisor``),
reading migration 051's ``inventory_transactions`` ledger.

This module recommends; it does not spend -- read this before anything else
--------------------------------------------------------------------------------
:func:`do_recommend_restock` is a **return-only advisor**. It performs **no
writes of any kind**: no ``inventory_items`` update, no ``inventory_transactions``
append, no ``kg_nodes``/``kg_edges`` row, no purchase order. It only ``SELECT``s
from ``inventory_transactions`` (inside a read-only ``scoped_pg_session``
transaction, used purely to establish the RLS namespace context -- not to hold
open any write) and returns a plain ``dict`` of recommendations for a caller to
act on. Creating the actual restock PO is Batch 137's concern
(``do_create_restock_po``); this module does not call it, import it, or know
it exists.

Velocity comes from the ledger, and only the ledger
-----------------------------------------------------
This wave was re-sequenced (see the wave's own orchestrator amendment) to
depend on ``inventory_transactions`` (Wave 11, migration 051) instead of
``inventory_items`` (Wave 2) precisely because ``inventory_items`` carries only
a *current* quantity, no movement history, and this wave needs consumption
*velocity*. Consequently this module reads **only** ``inventory_transactions``
-- it never queries ``inventory_items`` and never imports anything from
``stock.py``. Both the "current stock position" figure AND the "consumption
velocity" figure are reconstructed from the SAME ledger read:

* **``current_balance``** -- the net running quantity for a (namespace, sku[,
  location]), i.e. the sum of EVERY ``delta`` ever appended (positive and
  negative, every ``reason_category``). Because every ``inventory_items``
  writer appends its ledger row in the SAME transaction as the row it mutates
  (``transactions.py``'s ``append_transaction`` docstring), this sum is exactly
  ``inventory_items.qty_on_hand`` reconstructed from first principles --
  without this module ever touching that table. This is the quantity compared
  against the configured reorder point (see "Config is IP" below): the classic
  reorder-point meaning is "reorder when the stock POSITION drops below the
  threshold", not "reorder when consumption exceeds a threshold" -- a SKU
  *below* its reorder point is recommended, one above it is not (the wave's
  own acceptance framing).
* **``consumption_velocity_qty``** -- the total of the DEPLETING rows within
  ``consumption_lookback_days`` (config, see below) of "now". A depleting row
  is always a NEGATIVE-delta row, but *which* negative-delta rows count is
  **scope-dependent**. That split is TWO independent decisions, and they do
  NOT share a rationale -- each is argued separately below, because the
  argument for one is not valid for the other:

  - **``transfer_out`` counts location-scoped, and is EXCLUDED
    namespace-wide.** A transfer is internal movement between two of the
    tenant's own locations, so it always writes a PAIR of rows in the same
    transaction: a negative ``transfer_out`` at the source and a positive
    ``transfer_in`` at the destination (``stock.py``'s ``do_transfer_stock``;
    migration 051's table comment). For a **location-scoped** call
    (``params["location"]`` supplied) only the source half is in scope: that
    stock genuinely left THAT shelf and must be replenished THERE, so it is
    real demand at that location. For a **namespace-wide** call (``location``
    omitted -- the documented default) BOTH halves are in scope and the pair
    nets to zero: nothing was consumed, so counting the ``transfer_out`` half
    alone would report demand that does not exist.
  - **``adjustment`` counts in BOTH scopes.** This one is NOT covered by the
    netting argument above, and must not be read as if it were. Migration
    051's sign CHECK deliberately leaves ``adjustment`` unconstrained in sign
    ("a manual correction can go either way"), which makes it the ONLY
    category that can carry a NEGATIVE delta while being neither a transfer
    nor a consumption -- i.e. shrinkage, breakage, and cycle-count
    write-downs. Unlike a ``transfer_out``, such a write-down has **no
    counterpart row** anywhere in the namespace to net against: the stock is
    simply gone, and the tenant has to buy it again. Excluding it
    namespace-wide would under-state demand and under-recommend restock in
    exactly the situation where stock is quietly disappearing, which is the
    situation a restock advisor exists to catch.

  The remaining categories need no argument at all, because the sign CHECK
  settles them: ``transfer_in`` (051) and ``goods_receipt`` (052, on this
  branch) are forced POSITIVE, so neither can ever BE a depleting row in
  either scope, and ``consumption`` is forced negative -- depletion by
  definition, in either scope.

  ``current_balance`` is unaffected by this split -- it always sums every row
  in scope, of every category and both signs. ``reason_category`` is surfaced
  in the rationale to explain WHY a row counted. The velocity figure is
  descriptive/audit context -- the concrete ledger rows a human can point to
  and ask "why did it say that" -- not itself the reorder trigger, which is
  the stock position above.

An unknown or foreign ``location`` is a caller error, not an empty warehouse
------------------------------------------------------------------------------
``params["location"]``, when supplied, is validated against ``stock_locations``
for the SAME ``namespace_id`` before any ledger row is read; a miss raises
``ValueError``. This is the module's only read of a table other than
``inventory_transactions``, and it exists because the failure mode is
otherwise silent and expensive: a stale, mistyped, or *another tenant's*
location id matches no ledger row, and an empty ledger read is
indistinguishable from a genuinely empty location -- so the advisor would
return ``current_balance 0.000``, ``recommended: true`` and a confident
rationale reading "ledger balance 0.000 ... BELOW the configured reorder
point" for EVERY configured SKU. "Restock everything" is the most expensive
possible wrong answer and must not be reachable by a typo.

**Scope every query with an explicit ``namespace_id = $ns``.** This has bitten
three prior waves (B67, B120, B130) precisely because the integration-test
pool is an owner/superuser role that BYPASSES FORCE RLS -- an unscoped query
still passes its own test and only leaks in production. Every SQL statement in
this module binds ``namespace_id`` as an explicit parameter; none rely on RLS
alone.

The work-order demand signal is a SEAM, not an implementation
-------------------------------------------------------------------
Field Tech (Module 12) does not exist yet -- there is no work-order table to
query, and this module builds none. The upcoming-demand term is injected as
:class:`WorkOrderDemandSignal`, a structural (``Protocol``) callable taking
``(engine, ns_uuid, sku, location_id)`` and returning a non-negative
``Decimal`` -- the additional quantity of *sku* expected to be consumed by
scheduled work orders. :func:`_no_work_order_demand` is the default: it always
returns zero ("no demand"), which is the only honest answer when Module 12
does not exist. A caller (or a future Module 12 wiring) passes a real
implementation via the ``work_order_demand`` keyword; the tests in
``tests/test_inventory_replenishment.py`` assert against a fake, never against
an invented work-order schema.

The demand-signal call happens strictly OUTSIDE the ``scoped_pg_session``
block (see :func:`do_recommend_restock`'s two-phase structure) -- that context
manager's own docstring forbids slow external I/O (Mongo/HTTP/LLM calls) while
a transaction is held open, and an injected seam's latency is, by
construction, unknown to this module.

Config is IP: ``inventory-reorder-points.json`` and its fallback policy
------------------------------------------------------------------------------
Mirrors how ``transactions.py``'s ``load_inventory_valuation_config`` loads
``inventory-valuation.json`` -- a bare JSON read, no DB, no config class,
GLOBAL (not namespace-scoped) for this wave, same choice that file's own
``_comment`` makes for itself.

The file's shape makes the fallback decision explicit rather than requiring
this module to invent one: ``reorder_points`` is a mapping of ONLY the SKUs a
tenant has configured a threshold for. A SKU that is not a key in that mapping
has, by the file's own shape, no policy at all -- there is nothing to compare
its ledger balance against. This module's fallback is therefore: report that
SKU with ``reorder_point: null`` and ``recommended: false`` (never a guessed
threshold, never silently dropped from the result -- the caller can see
exactly which SKUs it has no opinion on and why). This case can only be
reached via an explicit ``params["sku"]`` request for an unconfigured SKU --
when ``sku`` is omitted, the set of SKUs evaluated is exactly
``reorder_points``'s own key set, so every entry returned always has a
configured threshold.

``consumption_lookback_days`` (default 30, matching the not-yet-implemented
``NCE_INVENTORY_REORDER_LOOKBACK_DAYS`` env key's planned default -- see
``docs/engines/inventory-admin.md`` §4.1) lives in this same JSON file rather
than ``nce/config.py`` because that file is explicitly out of scope for this
wave (see the wave's own orchestrator amendments) -- carried as config-as-IP
instead of invented as a Python constant.

Only an ABSENT ``consumption_lookback_days`` falls back to 30 (see
:func:`_parse_lookback_days`); a value that is present but non-positive is
REJECTED, never silently replaced. ``0`` and ``30`` are different instructions
and must not produce the same answer -- a falsy-coalescing default would
report ``consumption_lookback_days: 30`` back to a caller whose config said
``0``. A negative value is worse: it puts the cutoff in the FUTURE, so
``consumption_velocity_qty`` is permanently ``0.000`` and ``ledger_rows``
permanently empty, with no error and a rationale that reads exactly as if
nothing had been consumed. The window is bounded ABOVE as well
(:data:`_MAX_LOOKBACK_DAYS`, 100 years) and for the same reason the lower
bound exists -- to fail as the documented ``ValueError`` rather than as
something else: past that point ``now - timedelta(days=N)`` is simply not
representable and raises ``OverflowError`` out of date arithmetic, an
exception type neither this parser nor :func:`do_recommend_restock` documents.

Dependency direction (uncle-bob-craft)
-------------------------------------------
This module imports only ``asyncpg`` and ``nce.db_utils.scoped_pg_session`` --
no web/HTTP/admin framework imports, and nothing from
``nce.vertical_modules.inventory.stock`` (this module never reads
``inventory_items``, see above) or ``.rma``. ``NCEEngine`` is imported under
``TYPE_CHECKING`` only, matching ``stock.py``/``transactions.py``/``rma.py``'s
own convention.

Decimal coercion is duplicated from ``transactions.py``, not imported
------------------------------------------------------------------------------
``transactions.py``'s ``_as_decimal``/``_quantise_qty`` are module-private
(leading underscore); this module carries its own small, adapted copies
rather than importing them across modules -- the same duplication-over-
cross-module-private-import choice ``rma.py``'s own docstring already makes
(and argues for at length) for its own copies of the same two helpers.

Registration is deliberately NOT this wave's job
-----------------------------------------------------
``do_recommend_restock`` is not registered as an MCP tool or a REST route
here. This is Module 11's established pattern, not an oversight:
``do_record_rma`` (Batch 138, on ``main``) says so in its own module
docstring at ``nce/vertical_modules/inventory/rma.py:82``. Registration of
Inventory's ``do_*`` entry points is Batch 138a's (``inventory-surface-
completion``) single concern, and the exact tool-count assertion is Batch
140's -- editing ``nce/tool_registry.py`` here would collide with both.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.replenishment")

# ---------------------------------------------------------------------------
# Decimal coercion -- Decimal end-to-end, quantised BEFORE binding to any
# query / comparison (see module docstring's "Decimal coercion is duplicated"
# section).
# ---------------------------------------------------------------------------

_QTY_SCALE: Decimal = Decimal("0.001")
_ZERO_QTY: Decimal = Decimal("0.000")

_DEFAULT_LOOKBACK_DAYS = 30

# Upper bound on ``consumption_lookback_days``. The BINDING constraint is what
# ``datetime``/``timedelta`` arithmetic can represent -- ``datetime.now(utc) -
# timedelta(days=N)`` raises ``OverflowError`` (not ``ValueError``) once N
# passes ~739_800 (``datetime.min``), again at 10**9 (``timedelta.max.days``),
# and again at 10**12 (int too large for a C int) -- so the ceiling is set two
# orders of magnitude INSIDE the nearest of those, at 36_525 days (100 years,
# leap days included), where the failure is a clean ValueError from this
# parser rather than an OverflowError from line-665 date math. No real
# consumption lookback approaches a century.
_MAX_LOOKBACK_DAYS = 36_525

# Which ``reason_category`` values count toward ``consumption_velocity_qty``.
# Both sets are only ever intersected with "delta < 0", so the CHECK-forced
# always-positive ``transfer_in`` is omitted from both: it can never BE a
# depleting row, and listing it would suggest a decision was made about it.
# ``transfer_out`` is the ONLY difference between the two sets -- see the
# module docstring's velocity section for the two SEPARATE arguments (the
# transfer_out/transfer_in netting one, which is why transfer_out is dropped
# namespace-wide, and the no-counterpart-row one, which is why a negative
# ``adjustment`` -- shrinkage/breakage/cycle-count write-down, the only
# non-transfer non-consumption negative 051 permits -- is kept in BOTH).
#
# NOT a gap: ``goods_receipt`` (migration 052, Batch 132) is omitted, once
# that migration is applied, for exactly the reason ``transfer_in`` is,
# not because the category is unknown here. 052 widens 051's sign CHECK with
# ``(reason_category = 'goods_receipt' AND delta > 0)`` -- positive-only -- so
# a ``goods_receipt`` row can never satisfy the "delta < 0" filter these sets
# are intersected with, and adding it to either set would be a no-op that
# suggested a decision had been made about it. A "negative goods-receipt
# correction" is unrepresentable by construction: the CHECK refuses it, and
# such a correction is written as an ``adjustment`` row, which IS counted.
_LOCATION_SCOPED_DEPLETION_CATEGORIES: frozenset[str] = frozenset(
    {"consumption", "adjustment", "transfer_out"}
)
_NAMESPACE_WIDE_DEPLETION_CATEGORIES: frozenset[str] = frozenset({"consumption", "adjustment"})


def _depletion_categories_for_scope(location_id: UUID | None) -> frozenset[str]:
    """The depletion set for this call's scope.

    ``transfer_out`` is the only category that differs, and it differs because
    a transfer writes a PAIR of rows: location-scoped, only the negative half
    is in scope and that stock really did leave the shelf, so it is demand
    there; namespace-wide, both halves are in scope and net to zero, so
    counting the ``transfer_out`` alone would invent demand.

    A negative ``adjustment`` (shrinkage, breakage, a cycle-count write-down
    -- 051's sign CHECK makes ``adjustment`` the only non-transfer,
    non-consumption category that can go negative) is counted in BOTH scopes:
    it has no counterpart row to net against, so the stock is gone from the
    namespace and has to be bought again. The netting argument above does not
    apply to it.
    """
    if location_id is not None:
        return _LOCATION_SCOPED_DEPLETION_CATEGORIES
    return _NAMESPACE_WIDE_DEPLETION_CATEGORIES


def _as_decimal(value: Any, where: str) -> Decimal:
    """Coerce a caller/config-supplied number to an exact, finite ``Decimal``.

    ``bool`` is rejected before the ``int`` branch (``isinstance(True, int)``
    is ``True`` in Python); a float is converted via ``Decimal(str(x))``,
    never ``Decimal(x)`` -- the latter imports the binary-float representation
    error (``stock.py``'s "Quantity precision" section argues this at
    length)."""
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
    """Round to ``inventory_transactions.delta``'s own column scale (3dp),
    ties away from zero -- same scale and rounding as ``transactions.py``'s /
    ``rma.py``'s own ``_quantise_qty``."""
    try:
        return value.quantize(_QTY_SCALE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: quantity is too large to express to 3dp: {value!r}") from exc


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"'{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _as_optional_location_uuid(raw: Any) -> UUID | None:
    """``location`` is optional -- ``None``/empty means "every location in
    the namespace"."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise ValueError(
            f"do_recommend_restock: 'location' must be a UUID string, got {raw!r}"
        ) from exc


def _as_optional_sku(raw: Any) -> str | None:
    """``sku`` is optional -- ``None``/blank means "every configured SKU"
    (see module docstring's "Config is IP" section)."""
    if raw is None:
        return None
    sku = str(raw).strip()
    return sku or None


# ---------------------------------------------------------------------------
# Reorder-points config loader -- reads
# nce/config_data/inventory-reorder-points.json (no config class), mirrors
# transactions.py's load_inventory_valuation_config().
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"
_REORDER_POINTS_CONFIG_FILENAME = "inventory-reorder-points.json"


def load_inventory_reorder_points_config() -> dict[str, Any]:
    """Load and return the contents of ``inventory-reorder-points.json``.

    Returns
    -------
    dict with keys ``consumption_lookback_days`` (int) and ``reorder_points``
    (mapping of SKU -> configured reorder-point quantity). Global -- not
    namespace-scoped -- for this wave (see the file's own ``_comment`` and
    this module's "Config is IP" docstring section).
    """
    path = _CONFIG_DATA_DIR / _REORDER_POINTS_CONFIG_FILENAME
    with path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    return config


def _parse_lookback_days(raw: Any) -> int:
    """Coerce ``consumption_lookback_days`` to a whole number of days inside
    ``1 .. _MAX_LOOKBACK_DAYS``.

    ABSENT (``None``/key missing) is the ONLY case that falls back to
    :data:`_DEFAULT_LOOKBACK_DAYS`. A present-but-non-positive value is
    rejected, never coalesced: ``0`` is falsy but is not "unset", and
    returning 30 for a config that says 0 makes the response's own
    ``consumption_lookback_days`` field contradict the file it came from. A
    negative value puts the lookback cutoff in the FUTURE -- velocity pinned
    at ``0.000`` and ``ledger_rows`` empty forever, with a rationale that
    reads as if nothing was consumed. See the module docstring's config
    section.

    BOTH bounds are checked HERE and both raise ``ValueError``, which is what
    :func:`do_recommend_restock`'s ``Raises`` block promises for a malformed
    config value. An unchecked UPPER bound does not make the call succeed --
    it makes it raise the WRONG exception type, later, out of
    ``datetime.now(timezone.utc) - timedelta(days=...)``: ``OverflowError``
    ("date value out of range") at 10**6, ``OverflowError`` (past
    ``timedelta.max.days``) at 10**9, and ``OverflowError`` ("Python int too
    large to convert to C int") at 10**12. See :data:`_MAX_LOOKBACK_DAYS` for
    the ceiling and why it sits where it does.
    """
    where = "inventory-reorder-points.json.consumption_lookback_days"
    if raw is None:
        return _DEFAULT_LOOKBACK_DAYS
    if isinstance(raw, bool):
        raise ValueError(f"{where}: bool is not a number of days, got {raw!r}")
    if isinstance(raw, int):
        days = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            raise ValueError(f"{where}: must be a whole number of days, got {raw!r}")
        days = int(raw)
    else:
        raise ValueError(f"{where}: expected an int, got {type(raw).__name__} {raw!r}")
    if days <= 0:
        raise ValueError(
            f"{where}: must be a positive number of days, got {days!r} -- 0 would make "
            "the lookback window empty while the response still reported a window, and "
            "a negative value puts the cutoff in the future, pinning "
            "consumption_velocity_qty at 0.000 and ledger_rows at [] with no error."
        )
    if days > _MAX_LOOKBACK_DAYS:
        raise ValueError(
            f"{where}: must be at most {_MAX_LOOKBACK_DAYS} days "
            f"(100 years), got {days!r} -- a larger window is not a longer lookback, it is "
            "a value 'now - timedelta(days=...)' cannot represent, which surfaces as an "
            "OverflowError from date arithmetic instead of the ValueError this parser "
            "documents for a malformed config value."
        )
    return days


def _parse_reorder_points(raw: Mapping[str, Any]) -> dict[str, Decimal]:
    """Coerce the config's raw ``{sku: number}`` mapping into
    ``{sku: Decimal}``, quantised to the ledger's own 3dp scale."""
    return {
        str(sku): _quantise_qty(
            _as_decimal(value, f"inventory-reorder-points.json.reorder_points[{sku!r}]"),
            f"inventory-reorder-points.json.reorder_points[{sku!r}]",
        )
        for sku, value in raw.items()
    }


# ---------------------------------------------------------------------------
# The work-order demand signal SEAM (module docstring's own section).
# ---------------------------------------------------------------------------


class WorkOrderDemandSignal(Protocol):
    """Structural seam for Field Tech (Module 12) work-order demand.

    Module 12 does not exist yet, so there is no work-order table to query
    (see module docstring). An implementation returns the additional
    quantity of *sku* expected to be consumed by upcoming/scheduled work
    orders at *location_id* (or across the whole namespace when
    *location_id* is ``None``) as a non-negative ``Decimal``.
    :func:`_no_work_order_demand` is the default -- always zero.
    """

    async def __call__(
        self,
        engine: NCEEngine,
        ns_uuid: UUID,
        sku: str,
        location_id: UUID | None,
    ) -> Decimal: ...


async def _no_work_order_demand(
    engine: NCEEngine,
    ns_uuid: UUID,
    sku: str,
    location_id: UUID | None,
) -> Decimal:
    """Default :class:`WorkOrderDemandSignal` -- always "no demand". Module 12
    (Field Tech / work orders) does not exist yet; there is no schema to
    query, so the honest default is zero, never a guess."""
    return _ZERO_QTY


# ---------------------------------------------------------------------------
# Ledger read -- the SOLE data source for both current position and velocity
# (module docstring's "Velocity comes from the ledger, and only the ledger").
# ---------------------------------------------------------------------------


async def _assert_location_in_namespace(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    location_id: UUID,
) -> None:
    """Reject a ``location`` that is not one of THIS namespace's
    ``stock_locations`` rows -- unknown id, random UUID, or another tenant's
    location id (the composite FK makes the last one unreachable from the
    ledger, so it reads as an empty warehouse rather than as the error it is).

    Raises ``ValueError`` rather than returning zeros: see the module
    docstring's "An unknown or foreign ``location`` is a caller error" section
    -- a silent phantom zero recommends restocking every configured SKU.
    Binds ``namespace_id`` explicitly, like every other query here.
    """
    found = await conn.fetchval(
        """
        SELECT 1
        FROM stock_locations
        WHERE id = $1::uuid AND namespace_id = $2::uuid
        """,
        str(location_id),
        str(ns_uuid),
    )
    if found is None:
        raise ValueError(
            f"do_recommend_restock: 'location' {location_id} is not a stock_locations "
            f"id in namespace {ns_uuid} -- an unknown, stale, or foreign location is a "
            "caller error, not an empty warehouse (it would otherwise report a phantom "
            "zero balance and recommend restocking every configured SKU)."
        )


async def _fetch_ledger_rows(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    sku: str,
    location_id: UUID | None,
) -> list[asyncpg.Record]:  # type: ignore[type-arg]
    """Every ``inventory_transactions`` row ever appended for this (namespace,
    sku[, location]), oldest first. Every branch binds ``namespace_id`` as an
    explicit ``$1`` parameter -- never relies on RLS alone (module docstring's
    scoping-discipline section; the B67/B120/B130 lesson)."""
    if location_id is not None:
        return await conn.fetch(
            """
            SELECT id, delta, reason_category, created_at
            FROM inventory_transactions
            WHERE namespace_id = $1::uuid AND sku = $2 AND location_id = $3::uuid
            ORDER BY created_at ASC, id ASC
            """,
            str(ns_uuid),
            sku,
            str(location_id),
        )
    return await conn.fetch(
        """
        SELECT id, delta, reason_category, created_at
        FROM inventory_transactions
        WHERE namespace_id = $1::uuid AND sku = $2
        ORDER BY created_at ASC, id ASC
        """,
        str(ns_uuid),
        sku,
    )


# ---------------------------------------------------------------------------
# Pure ledger math -- no DB, no asyncpg awareness (mirrors transactions.py's
# split between DB fetch and pure _compute_valuation).
# ---------------------------------------------------------------------------


class _LedgerMetrics(NamedTuple):
    current_balance: Decimal
    velocity_qty: Decimal
    velocity_rows: list[Mapping[str, Any]]


def _compute_ledger_metrics(
    rows: Sequence[Mapping[str, Any]],
    lookback_cutoff: datetime,
    *,
    depletion_categories: frozenset[str],
) -> _LedgerMetrics:
    """Pure over already-fetched ``{"id", "delta", "reason_category",
    "created_at"}`` rows, oldest first.

    ``current_balance`` sums EVERY row (all reason categories, all time) --
    the ledger-reconstructed stock position (see module docstring) -- and is
    NOT affected by *depletion_categories*.

    ``velocity_qty``/``velocity_rows`` total only the negative-delta rows
    inside the lookback window WHOSE ``reason_category`` is in
    *depletion_categories* -- the consumption-velocity figure cited in the
    rationale. The caller supplies that set from
    :func:`_depletion_categories_for_scope`; it is required (not defaulted)
    precisely because "does a ``transfer_out`` count?" has no scope-free
    answer.
    """
    current_balance = _ZERO_QTY
    velocity_qty = _ZERO_QTY
    velocity_rows: list[Mapping[str, Any]] = []
    for row in rows:
        delta: Decimal = row["delta"]
        current_balance += delta
        if (
            delta < _ZERO_QTY
            and row["reason_category"] in depletion_categories
            and row["created_at"] >= lookback_cutoff
        ):
            velocity_qty += -delta
            velocity_rows.append(row)
    return _LedgerMetrics(
        current_balance=current_balance,
        velocity_qty=velocity_qty,
        velocity_rows=velocity_rows,
    )


# ---------------------------------------------------------------------------
# Rationale assembly -- pure, cites concrete ledger rows (module docstring /
# wave acceptance: "a rationale citing the concrete ledger rows the velocity
# came from").
# ---------------------------------------------------------------------------


def _build_recommendation(
    *,
    sku: str,
    location_id: UUID | None,
    reorder_point: Decimal | None,
    metrics: _LedgerMetrics,
    demand_qty: Decimal,
    projected_position: Decimal,
    recommended: bool,
    lookback_days: int,
) -> dict[str, Any]:
    ledger_rows_out = [
        {
            "id": str(row["id"]),
            "delta": row["delta"],
            "reason_category": row["reason_category"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in metrics.velocity_rows
    ]
    row_citations = (
        ", ".join(f"{r['id']}[{r['reason_category']}:{r['delta']}]" for r in ledger_rows_out)
        or "no depleting rows in the lookback window"
    )

    if reorder_point is None:
        rationale = (
            f"{sku}: no reorder point configured in inventory-reorder-points.json -- "
            f"not evaluated for restock (ledger balance {metrics.current_balance}, "
            f"{lookback_days}d consumption {metrics.velocity_qty} from rows: {row_citations})."
        )
    else:
        rationale = (
            f"{sku}: ledger balance {metrics.current_balance} minus {demand_qty} projected "
            f"work-order demand = {projected_position} projected position, "
            f"{'BELOW' if recommended else 'at/above'} the configured reorder point of "
            f"{reorder_point}. {lookback_days}d consumption velocity "
            f"{metrics.velocity_qty} from rows: {row_citations}."
        )

    return {
        "sku": sku,
        "location_id": str(location_id) if location_id is not None else None,
        "reorder_point": reorder_point,
        "current_balance": metrics.current_balance,
        "consumption_velocity_qty": metrics.velocity_qty,
        "demand_qty": demand_qty,
        "projected_position": projected_position,
        "recommended": recommended,
        "rationale": rationale,
        "ledger_rows": ledger_rows_out,
    }


# ---------------------------------------------------------------------------
# Public: do_recommend_restock -- return-only advisor. NO writes (module
# docstring's "This module recommends; it does not spend" section).
# ---------------------------------------------------------------------------


async def do_recommend_restock(
    engine: NCEEngine,
    params: dict[str, Any],
    *,
    work_order_demand: WorkOrderDemandSignal = _no_work_order_demand,
) -> dict[str, Any]:
    """Per-SKU restock recommendations from ledger-derived stock position +
    consumption velocity + injected work-order demand, against configured
    reorder points. **Writes nothing** -- see module docstring.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "location":     str | UUID | None,  # optional -- restrict to one
                                                  # stock_locations id OF THIS
                                                  # namespace (validated; an
                                                  # unknown/foreign id raises);
                                                  # omitted means every location
            "sku":          str | None,  # optional -- evaluate only this SKU
                                          # (even if unconfigured); omitted
                                          # means every SKU configured in
                                          # inventory-reorder-points.json
        }``

        Supplying ``location`` also changes what counts as depletion: a
        location-scoped call counts ``transfer_out`` rows, a namespace-wide
        one does not (the transfer's ``transfer_in`` half is inside that
        scope and nets it out). Negative ``adjustment`` rows -- shrinkage,
        breakage, cycle-count write-downs -- count in BOTH scopes; they have
        no counterpart row to net against (module docstring's velocity
        section).
    work_order_demand:
        Injected :class:`WorkOrderDemandSignal` seam (keyword-only). Defaults
        to :func:`_no_work_order_demand` ("no demand") -- Module 12 (Field
        Tech / work orders) does not exist yet.

    Returns
    -------
    dict
        ``{"ok": True, "namespace_id", "location_id", "consumption_lookback_days",
        "recommendations": [ {sku, location_id, reorder_point, current_balance,
        consumption_velocity_qty, demand_qty, projected_position, recommended,
        rationale, ledger_rows}, ... ]}``. Every entry carries a rationale
        citing the concrete ledger rows its velocity figure came from (see
        module docstring). A SKU absent from ``inventory-reorder-points.json``
        is only ever returned when explicitly requested via ``params["sku"]``
        -- it comes back with ``reorder_point: null`` and
        ``recommended: false`` (see module docstring's "Config is IP"
        section), never a guessed threshold.

    Raises
    ------
    ValueError
        ``namespace_id`` missing; ``location`` not a UUID, or a UUID that is
        not a ``stock_locations`` row in this namespace; or a malformed
        config value (a ``consumption_lookback_days`` outside
        ``1 .. _MAX_LOOKBACK_DAYS``, a non-numeric reorder point). Both ends
        of that range are enforced in :func:`_parse_lookback_days`, so an
        out-of-range window never reaches the ``timedelta`` on the next page
        and can never surface as an ``OverflowError`` instead.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    location_id = _as_optional_location_uuid(params.get("location"))
    sku_filter = _as_optional_sku(params.get("sku"))

    config = load_inventory_reorder_points_config()
    lookback_days = _parse_lookback_days(config.get("consumption_lookback_days"))
    reorder_points = _parse_reorder_points(config.get("reorder_points") or {})

    skus_to_evaluate = [sku_filter] if sku_filter else sorted(reorder_points)
    lookback_cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    depletion_categories = _depletion_categories_for_scope(location_id)

    # Phase 1: ledger reads only, inside ONE scoped_pg_session (RLS context
    # set once for every SELECT). No slow external I/O here -- see module
    # docstring's "work-order demand signal" section on why the seam call is
    # deliberately kept OUTSIDE this block.
    ledger_by_sku: dict[str, list[dict[str, Any]]] = {}
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        if location_id is not None:
            # BEFORE any ledger read, and unconditionally -- an unknown or
            # foreign location must fail loudly even when the SKU set is empty.
            await _assert_location_in_namespace(conn, ns_uuid, location_id)
        for sku in skus_to_evaluate:
            db_rows = await _fetch_ledger_rows(conn, ns_uuid, sku, location_id)
            ledger_by_sku[sku] = [
                {
                    "id": r["id"],
                    "delta": r["delta"],
                    "reason_category": r["reason_category"],
                    "created_at": r["created_at"],
                }
                for r in db_rows
            ]

    # Phase 2: pure math + the injected (possibly slow) demand-signal seam,
    # OUTSIDE any open transaction/connection.
    recommendations: list[dict[str, Any]] = []
    for sku in skus_to_evaluate:
        metrics = _compute_ledger_metrics(
            ledger_by_sku[sku],
            lookback_cutoff,
            depletion_categories=depletion_categories,
        )

        demand_qty = await work_order_demand(engine, ns_uuid, sku, location_id)
        demand_qty = _quantise_qty(_as_decimal(demand_qty, "work_order_demand"), "demand_qty")
        if demand_qty < _ZERO_QTY:
            # Defensive only -- a well-behaved seam never returns a negative
            # demand; a misbehaving one must not silently inflate the
            # projected position.
            demand_qty = _ZERO_QTY

        reorder_point = reorder_points.get(sku)
        projected_position = metrics.current_balance - demand_qty
        recommended = reorder_point is not None and projected_position < reorder_point

        recommendations.append(
            _build_recommendation(
                sku=sku,
                location_id=location_id,
                reorder_point=reorder_point,
                metrics=metrics,
                demand_qty=demand_qty,
                projected_position=projected_position,
                recommended=recommended,
                lookback_days=lookback_days,
            )
        )

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "location_id": str(location_id) if location_id is not None else None,
        "consumption_lookback_days": lookback_days,
        "recommendations": recommendations,
    }
