"""Tests for the Inventory engine's predictive replenishment Advisor (Module
11, Wave 6 — Batch 134 — ``nce/vertical_modules/inventory/replenishment.py``).

Covers:

  1. Pure-logic validation (no DB) — ``do_recommend_restock``'s
     required-field validation, exercised through the PUBLIC function before
     it ever touches ``engine.pg_pool`` (mirrors
     ``test_inventory_transactions.py``'s ``_DummyEngine`` pattern).
  2. Pure ledger math (no DB) — ``_compute_ledger_metrics`` called directly:
     the running balance sums every row regardless of category, the
     consumption-velocity figure only totals depleting rows INSIDE the
     lookback window, rows outside the window are excluded from both the
     velocity figure and the cited rows, the depletion set itself is
     scope-dependent (``_depletion_categories_for_scope``), and a NEGATIVE
     ``adjustment`` (shrinkage) counts as depletion in the namespace-wide
     scope too — it has no counterpart row to net against.
  3. Config loading/parsing (no DB) — the on-disk
     ``inventory-reorder-points.json`` has the expected shape,
     ``_parse_reorder_points`` quantises raw JSON numbers to the ledger's 3dp
     scale, and ``_parse_lookback_days`` distinguishes an ABSENT
     ``consumption_lookback_days`` (falls back to 30) from a present-but-
     out-of-range one — rejected at BOTH ends, 0 and negative below,
     ``_MAX_LOOKBACK_DAYS + 1`` and 10**6/10**9/10**12 above (each of which
     otherwise reaches ``timedelta`` and raises ``OverflowError``, not the
     documented ``ValueError``).
  4. The work-order demand SEAM's default (no DB) — ``_no_work_order_demand``
     always returns zero.
  5. Integration (``@pytest.mark.integration``, live Postgres) — the wave's
     own acceptance list, verbatim:
     - a SKU below its reorder point is recommended, one above it is not
       (one test, two SKUs, one call — see
       ``test_recommend_restock_below_reorder_point_is_recommended_above_is_not``);
     - the injected work-order demand signal (a fake, never an invented
       work-order schema) changes the outcome;
     - the rationale names the REAL ledger rows the velocity came from, and
       excludes rows outside the configured lookback window;
     - a second namespace's ledger rows never influence the first
       namespace's recommendation or appear in its rationale (every query in
       the module binds an explicit ``namespace_id`` — the B67/B120/B130
       lesson);
     - the documented fallback for a SKU absent from
       ``inventory-reorder-points.json`` (``reorder_point: null``,
       ``recommended: false``, never a guessed threshold);
     - the CONFIGURED ``consumption_lookback_days`` is the window actually
       applied and the one reported back (5n) — at 7 and 365 days, neither of
       which is the 30-day default every other windowed test uses.

Each integration test is written so that DELETING the predicate or guard it
claims to cover makes it FAIL — a happy-path assertion passes identically
against code with no guard at all, which is how this file's first version
shipped a cross-namespace test that could not detect a cross-namespace leak.
Specifically:

  - the isolation test (5d) passes NO ``location``, because
    ``stock_locations`` ids are per-namespace and
    ``inventory_transactions``' composite FK forbids a cross-tenant location
    ref — so a location filter alone already segregates the tenants and would
    keep the test green with ``namespace_id`` deleted from the query;
  - the two-location test (5g) pins the ``location_id`` predicate itself:
    the namespace-wide total and the per-location total are deliberately
    different numbers;
  - the transfer-pair test (5h) pins the scope-dependent depletion set;
  - the clamp test (5j) pins the negative-demand guard by asserting the
    recommendation it rescues;
  - the phantom-location tests (5i) pin the ``stock_locations`` validation
    that stops an unknown/foreign id reading as an empty warehouse;
  - the shrinkage test (5k) pins the ``adjustment`` half of the depletion
    set with a real negative-delta adjustment row and a velocity NUMBER
    (40.000 vs the 10.000 a ``{"consumption"}``-only set answers) — the
    ``_depletion_categories_for_scope`` assertions are a constant restatement
    and say so;
  - the boundary test (5l) pins the reorder comparison ITSELF at
    ``projected_position == reorder_point``, the one case ``<`` and ``<=``
    disagree about; every other balance in this file is strictly on one side;
  - the seam-scope test (5m) pins the ``location_id`` argument handed to
    ``work_order_demand`` — the fake RECORDS its arguments rather than
    ignoring them, so passing ``None`` for a location-scoped call is visible;
  - the per-entry ``location_id`` assertions in 5a/5g pin the field inside
    each recommendation (None / loc_a / loc_b are three different values), not
    just the one on the envelope;
  - the lookback test (5n) pins ``consumption_lookback_days`` itself — the
    figure, the cited-row COUNT and the reported window, at 7 and 365 days.
    Every other windowed test uses 30, which IS ``_DEFAULT_LOOKBACK_DAYS``, so
    all of them pass identically against code that ignores the config;
  - 5c/5h/5k assert the cited-row PAYLOAD, not just ``id`` — ``delta`` and
    ``reason_category`` in all three, and ``created_at`` in **5c only**, which
    is the one that seeds it explicitly instead of with ``now()`` (5h and 5k
    deliberately do not assert a wall-clock value they did not choose). Read
    distributively the earlier wording said all three pin ``created_at``, which
    is false: mutating it reddens one test, not three. ``reason_category`` is
    what the rationale uses to explain WHY a row counted
    (``replenishment.py``'s depletion-set comment), so relabelling 5h's
    ``transfer_out`` or 5k's shrinkage row as ``consumption`` must be visible —
    those two tests exist precisely to distinguish those categories;
  - the quantise pins: 5l passes the demand seam ``0.0004`` (below the
    ledger's own 3dp scale) at the reorder boundary, where dropping
    ``_quantise_qty`` flips ``recommended``; and
    ``test_parse_reorder_points_quantises_to_three_decimal_places`` asserts
    the exponent and a value that ROUNDS, because ``Decimal("8") ==
    Decimal("8.000")`` makes equality alone blind to a missing quantise.

SKUs in the integration tests are per-run unique (``_unique_sku``). The shared
integration database accumulates rows from every previous run across many
namespaces, so a fixed literal SKU makes "which rows came back" depend on that
history rather than on the seeding — which is precisely what let the original
isolation test pass while proving nothing.

Every integration test monkeypatches
``replenishment.load_inventory_reorder_points_config`` rather than mutating
the on-disk shared config file — same discipline
``test_inventory_transactions.py``'s valuation tests already use for
``load_inventory_valuation_config`` ("never the on-disk file — a test must
not mutate shared repo config").

This module never writes ``inventory_items``/``inventory_transactions``/
``kg_nodes``/``kg_edges`` — there is nothing to assert-was-not-written here
(unlike ``test_inventory_transactions.py``'s GL-boundary proof) because
``do_recommend_restock`` never opens a write-capable statement at all; the
NOT VERIFIED section at the bottom of the wave report names this explicitly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.vertical_modules.inventory import replenishment
from nce.vertical_modules.inventory.replenishment import (
    _DEFAULT_LOOKBACK_DAYS,
    _MAX_LOOKBACK_DAYS,
    WorkOrderDemandSignal,
    _as_optional_location_uuid,
    _as_optional_sku,
    _compute_ledger_metrics,
    _depletion_categories_for_scope,
    _no_work_order_demand,
    _parse_lookback_days,
    _parse_reorder_points,
    do_recommend_restock,
    load_inventory_reorder_points_config,
)

# The two scopes' depletion sets, resolved once so every call below states
# WHICH scope it is computing for (the parameter is required, not defaulted —
# "does a transfer_out count?" has no scope-free answer).
_LOCATION_SCOPED = _depletion_categories_for_scope(uuid.uuid4())
_NAMESPACE_WIDE = _depletion_categories_for_scope(None)

# ---------------------------------------------------------------------------
# 1. Pure-logic validation (no DB) — exercised through the PUBLIC function.
# ---------------------------------------------------------------------------


class _DummyEngine:
    """Stands in for NCEEngine in tests that never reach a DB call — the
    validation under test raises before ``engine.pg_pool`` is ever touched."""

    pg_pool = None


@pytest.mark.asyncio
async def test_do_recommend_restock_rejects_missing_namespace_id() -> None:
    with pytest.raises(ValueError, match="'namespace_id' is required"):
        await do_recommend_restock(_DummyEngine(), {})


@pytest.mark.asyncio
async def test_do_recommend_restock_rejects_bad_location_uuid() -> None:
    with pytest.raises(ValueError, match="'location' must be a UUID string"):
        await do_recommend_restock(
            _DummyEngine(), {"namespace_id": uuid.uuid4(), "location": "not-a-uuid"}
        )


def test_as_optional_location_uuid_none_and_blank_mean_every_location() -> None:
    assert _as_optional_location_uuid(None) is None
    assert _as_optional_location_uuid("") is None
    loc = uuid.uuid4()
    assert _as_optional_location_uuid(str(loc)) == loc
    assert _as_optional_location_uuid(loc) is loc


def test_as_optional_sku_none_and_blank_mean_every_configured_sku() -> None:
    assert _as_optional_sku(None) is None
    assert _as_optional_sku("   ") is None
    assert _as_optional_sku(" SKU-1 ") == "SKU-1"


# ---------------------------------------------------------------------------
# 2. Pure ledger math (no DB) — _compute_ledger_metrics called directly.
# ---------------------------------------------------------------------------


def test_compute_ledger_metrics_balance_sums_every_row_regardless_of_category() -> None:
    """current_balance must reflect ALL rows (any reason_category, any
    sign) — it reconstructs the ledger's own running position."""
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = [
        {
            "id": uuid.uuid4(),
            "delta": Decimal("10.000"),
            "reason_category": "transfer_in",
            "created_at": now,
        },
        {
            "id": uuid.uuid4(),
            "delta": Decimal("-3.000"),
            "reason_category": "consumption",
            "created_at": now,
        },
        {
            "id": uuid.uuid4(),
            "delta": Decimal("2.000"),
            "reason_category": "adjustment",
            "created_at": now,
        },
    ]
    metrics = _compute_ledger_metrics(
        rows,
        lookback_cutoff=now - timedelta(days=30),
        depletion_categories=_LOCATION_SCOPED,
    )
    assert metrics.current_balance == Decimal("9.000")
    # …and the depletion set does NOT narrow the balance — only the velocity.
    narrowed = _compute_ledger_metrics(
        rows,
        lookback_cutoff=now - timedelta(days=30),
        depletion_categories=_NAMESPACE_WIDE,
    )
    assert narrowed.current_balance == Decimal("9.000")


def test_compute_ledger_metrics_velocity_only_counts_negative_rows_in_window() -> None:
    """Location-scoped: velocity_qty totals only NEGATIVE-delta rows, and only
    those inside the lookback window — a row outside the window (however
    large) must not contribute, and must not appear in velocity_rows. A
    ``transfer_out`` DOES count here: stock left this location and must be
    replenished at this location."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    in_window_consumption = {
        "id": uuid.uuid4(),
        "delta": Decimal("-4.000"),
        "reason_category": "consumption",
        "created_at": now - timedelta(days=1),
    }
    in_window_transfer_out = {
        "id": uuid.uuid4(),
        "delta": Decimal("-1.500"),
        "reason_category": "transfer_out",
        "created_at": now - timedelta(days=2),
    }
    out_of_window = {
        "id": uuid.uuid4(),
        "delta": Decimal("-999.000"),
        "reason_category": "consumption",
        "created_at": now - timedelta(days=60),
    }
    positive_row = {
        "id": uuid.uuid4(),
        "delta": Decimal("50.000"),
        "reason_category": "transfer_in",
        "created_at": now - timedelta(days=1),
    }
    rows = [out_of_window, in_window_consumption, in_window_transfer_out, positive_row]

    metrics = _compute_ledger_metrics(
        rows, lookback_cutoff=cutoff, depletion_categories=_LOCATION_SCOPED
    )

    assert metrics.velocity_qty == Decimal("5.500"), "only the two in-window negative rows count"
    assert metrics.velocity_rows == [in_window_consumption, in_window_transfer_out]
    assert out_of_window not in metrics.velocity_rows
    assert positive_row not in metrics.velocity_rows
    # current_balance still reflects EVERY row, in-window or not, positive or not.
    assert metrics.current_balance == Decimal("-4.000") + Decimal("-1.500") + Decimal(
        "-999.000"
    ) + Decimal("50.000")


def test_compute_ledger_metrics_namespace_wide_scope_excludes_transfer_out() -> None:
    """Namespace-wide: a ``transfer_out`` is internal movement, not demand —
    its ``transfer_in`` counterpart is inside the same scope and the pair nets
    to zero. Only ``consumption`` rows count. Same rows as the location-scoped
    test above; only the depletion set changes."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    in_window_consumption = {
        "id": uuid.uuid4(),
        "delta": Decimal("-4.000"),
        "reason_category": "consumption",
        "created_at": now - timedelta(days=1),
    }
    in_window_transfer_out = {
        "id": uuid.uuid4(),
        "delta": Decimal("-1.500"),
        "reason_category": "transfer_out",
        "created_at": now - timedelta(days=2),
    }
    rows = [in_window_consumption, in_window_transfer_out]

    metrics = _compute_ledger_metrics(
        rows, lookback_cutoff=cutoff, depletion_categories=_NAMESPACE_WIDE
    )

    assert metrics.velocity_qty == Decimal("4.000"), (
        "namespace-wide, only the consumption row is demand — the transfer_out moved "
        "stock between two of this tenant's own locations"
    )
    assert metrics.velocity_rows == [in_window_consumption]
    assert in_window_transfer_out not in metrics.velocity_rows
    # The balance is unchanged by the narrower depletion set.
    assert metrics.current_balance == Decimal("-5.500")


def test_compute_ledger_metrics_namespace_wide_counts_a_negative_adjustment() -> None:
    """A negative ``adjustment`` — shrinkage, breakage, a cycle-count
    write-down — is depletion in the NAMESPACE-WIDE scope too, not only
    location-scoped.

    051's sign CHECK leaves ``adjustment`` unconstrained in sign, which makes
    it the only category that can carry a negative delta while being neither a
    transfer nor a consumption. The transfer-netting argument that justifies
    dropping ``transfer_out`` namespace-wide does NOT extend to it: a
    write-down has no counterpart row anywhere in the namespace, so the stock
    is genuinely gone and has to be bought again.

    The pure counterpart of integration test 5k — this one goes red without a
    database if the namespace-wide set is narrowed back to
    ``{"consumption"}`` (velocity 10.000 instead of 40.000).
    """
    now = datetime.now(timezone.utc)
    write_off = {
        "id": uuid.uuid4(),
        "delta": Decimal("-30.000"),
        "reason_category": "adjustment",
        "created_at": now - timedelta(days=1),
    }
    consumed = {
        "id": uuid.uuid4(),
        "delta": Decimal("-10.000"),
        "reason_category": "consumption",
        "created_at": now - timedelta(days=2),
    }
    positive_adjustment = {
        "id": uuid.uuid4(),
        "delta": Decimal("100.000"),
        "reason_category": "adjustment",
        "created_at": now - timedelta(days=3),
    }
    rows = [positive_adjustment, write_off, consumed]

    metrics = _compute_ledger_metrics(
        rows, lookback_cutoff=now - timedelta(days=30), depletion_categories=_NAMESPACE_WIDE
    )

    assert metrics.velocity_qty == Decimal("40.000"), (
        "30 written off + 10 consumed — a namespace-wide call that ignores the write-off "
        "under-states demand and under-recommends restock exactly where stock is "
        "quietly disappearing"
    )
    assert metrics.velocity_rows == [write_off, consumed]
    assert positive_adjustment not in metrics.velocity_rows, (
        "only NEGATIVE-delta rows deplete — a positive adjustment is a correction upward"
    )
    assert metrics.current_balance == Decimal("60.000")


def test_depletion_categories_for_scope_differs_by_scope() -> None:
    """The whole point of the parameter: the two scopes are not the same set.

    This test is a CONSTANT RESTATEMENT and is honest about it — it would be
    edited in lockstep with the constants and cannot, on its own, detect a
    wrong set. Its behavioural counterparts are the test directly above (pure)
    and integration test 5k (live rows), which pin the velocity NUMBER a
    negative adjustment produces.
    """
    location_scoped = _depletion_categories_for_scope(uuid.uuid4())
    namespace_wide = _depletion_categories_for_scope(None)

    assert location_scoped == frozenset({"consumption", "adjustment", "transfer_out"})
    assert namespace_wide == frozenset({"consumption", "adjustment"})

    assert location_scoped - namespace_wide == frozenset({"transfer_out"}), (
        "transfer_out is the ONLY category the two scopes may disagree about: it is the "
        "only one whose row comes in a pair that nets to zero inside the wider scope"
    )
    assert "transfer_out" in location_scoped, (
        "a location-scoped call must count transfer_out — that stock really left"
    )
    assert "transfer_out" not in namespace_wide, (
        "a namespace-wide call must NOT count transfer_out — its transfer_in counterpart "
        "is inside the same scope and nothing was consumed"
    )
    assert "adjustment" in namespace_wide, (
        "a negative adjustment is shrinkage/breakage/a write-down — it has NO counterpart "
        "row to net against, so it depletes the namespace and counts in BOTH scopes"
    )
    assert "transfer_in" not in location_scoped | namespace_wide, (
        "051's CHECK forces transfer_in positive, so it can never be a depleting row; "
        "listing it would imply a decision was made about it"
    )
    assert "goods_receipt" not in location_scoped | namespace_wide, (
        "goods_receipt (migration 052, Batch 132) is omitted, once that migration is "
        "applied, for the same reason transfer_in is, not because it is "
        "unknown here. 052 widens the sign CHECK with (reason_category = 'goods_receipt' "
        "AND delta > 0) — positive-only — so it can never satisfy the delta < 0 filter "
        "these sets are intersected with. A 'negative goods-receipt correction' is "
        "unrepresentable: the CHECK refuses it, and such a correction is written as an "
        "adjustment row, which IS counted"
    )


def test_compute_ledger_metrics_empty_rows_are_zero() -> None:
    now = datetime.now(timezone.utc)
    metrics = _compute_ledger_metrics(
        [], lookback_cutoff=now - timedelta(days=30), depletion_categories=_LOCATION_SCOPED
    )
    assert metrics.current_balance == Decimal("0.000")
    assert metrics.velocity_qty == Decimal("0.000")
    assert metrics.velocity_rows == []


# ---------------------------------------------------------------------------
# 3. Config loading/parsing (no DB).
# ---------------------------------------------------------------------------


def test_load_inventory_reorder_points_config_has_expected_shape() -> None:
    """Sanity check on the actual on-disk file this module ships — not
    monkeypatched. Every integration test below DOES monkeypatch the loader
    for its own scenario data (never mutates this shared file)."""
    config = load_inventory_reorder_points_config()
    assert isinstance(config.get("consumption_lookback_days"), int)
    assert isinstance(config.get("reorder_points"), dict)


def test_parse_reorder_points_quantises_to_three_decimal_places() -> None:
    """Equality ALONE cannot see the quantise: ``Decimal("5") ==
    Decimal("5.000")`` is ``True``, so a version of ``_parse_reorder_points``
    with ``_quantise_qty`` deleted passes an equality-only assertion
    identically. Pinned the two ways it CAN be seen — the EXPONENT (the scale
    ``inventory_transactions.delta``'s own ``NUMERIC(18,3)`` column carries,
    and the scale every balance it is compared against is expressed in), and a
    value that actually ROUNDS: ``2.3456`` is not equal to ``2.346`` under any
    comparison, so the reorder point a raw config number becomes is a
    different NUMBER without the quantise, not merely a differently-scaled one.
    """
    parsed = _parse_reorder_points({"SKU-A": 5, "SKU-B": 2.5, "SKU-C": 2.3456})
    assert parsed == {
        "SKU-A": Decimal("5.000"),
        "SKU-B": Decimal("2.500"),
        "SKU-C": Decimal("2.346"),
    }, "2.3456 must ROUND (half-up) to the ledger's 3dp scale, not survive as 2.3456"
    assert [str(v) for v in parsed.values()] == ["5.000", "2.500", "2.346"], (
        "the scale itself is load-bearing and equality is blind to it — a bare "
        "Decimal(5) compares equal to Decimal('5.000') while printing as '5'"
    )


def test_parse_lookback_days_absent_falls_back_to_the_documented_default() -> None:
    """ABSENT is the only case that defaults — 30, matching the planned
    NCE_INVENTORY_REORDER_LOOKBACK_DAYS env key (inventory-admin.md §4.1)."""
    assert _parse_lookback_days(None) == _DEFAULT_LOOKBACK_DAYS == 30


def test_parse_lookback_days_accepts_a_positive_value_unchanged() -> None:
    assert _parse_lookback_days(7) == 7
    assert _parse_lookback_days(30.0) == 30


def test_parse_lookback_days_rejects_zero_rather_than_silently_defaulting() -> None:
    """0 is falsy but it is NOT "unset". A falsy-coalescing default would
    answer a config that says 0 with a 30-day window AND report
    ``consumption_lookback_days: 30`` back, contradicting the file it read."""
    with pytest.raises(ValueError, match="must be a positive number of days"):
        _parse_lookback_days(0)


def test_parse_lookback_days_rejects_negative_which_would_put_the_cutoff_in_the_future() -> None:
    """A negative lookback makes ``now - timedelta(days=-5)`` a FUTURE cutoff:
    no row can ever satisfy it, so velocity is permanently 0.000 and
    ledger_rows permanently empty — with a rationale reading exactly as if
    nothing had been consumed."""
    with pytest.raises(ValueError, match="must be a positive number of days"):
        _parse_lookback_days(-5)


def test_parse_lookback_days_accepts_the_ceiling_and_rejects_one_day_past_it() -> None:
    """The upper bound is checked on the same side of the call as the lower
    one, so both ends of the range fail as the documented ``ValueError``."""
    assert _parse_lookback_days(_MAX_LOOKBACK_DAYS) == _MAX_LOOKBACK_DAYS == 36_525
    with pytest.raises(ValueError, match="must be at most"):
        _parse_lookback_days(_MAX_LOOKBACK_DAYS + 1)


def test_parse_lookback_days_rejects_huge_values_as_valueerror_not_overflowerror() -> None:
    """Without the ceiling these three do NOT raise here — they sail through
    the parser and blow up later inside
    ``datetime.now(timezone.utc) - timedelta(days=lookback_days)``, each with a
    different ``OverflowError`` (measured on CPython 3.11): ``10**6`` -> "date
    value out of range"; ``10**9`` -> "days=1000000000; must have magnitude <=
    999999999"; ``10**12`` -> "Python int too large to convert to C int".

    ``OverflowError`` derives from ``ArithmeticError``, NOT from
    ``ValueError``, so ``pytest.raises(ValueError)`` genuinely discriminates:
    remove the ceiling and this test errors out rather than passing.
    """
    for huge in (10**6, 10**9, 10**12):
        with pytest.raises(ValueError, match="must be at most"):
            _parse_lookback_days(huge)


def test_parse_lookback_days_rejects_bool_and_non_numeric() -> None:
    with pytest.raises(ValueError, match="bool is not a number of days"):
        _parse_lookback_days(True)
    with pytest.raises(ValueError, match="expected an int"):
        _parse_lookback_days("30")
    with pytest.raises(ValueError, match="whole number of days"):
        _parse_lookback_days(1.5)


@pytest.mark.asyncio
async def test_do_recommend_restock_surfaces_a_non_positive_lookback_before_touching_the_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the PUBLIC function: the rejection happens during
    config parsing, before ``engine.pg_pool`` is ever touched (``_DummyEngine``
    has none) — so a bad lookback can never reach a query."""
    for bad in (0, -5):
        monkeypatch.setattr(
            replenishment,
            "load_inventory_reorder_points_config",
            lambda bad=bad: {"consumption_lookback_days": bad, "reorder_points": {"SKU-A": 5}},
        )
        with pytest.raises(ValueError, match="must be a positive number of days"):
            await do_recommend_restock(_DummyEngine(), {"namespace_id": uuid.uuid4()})


@pytest.mark.asyncio
async def test_do_recommend_restock_surfaces_an_oversized_lookback_as_the_documented_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public function's ``Raises`` block promises ``ValueError`` for a
    malformed config value, and this is the line that used to break that
    promise: ``lookback_cutoff = datetime.now(timezone.utc) -
    timedelta(days=lookback_days)``.

    Exercised through the PUBLIC function so the guarantee under test is the
    documented one, not the parser's internals — and with ``_DummyEngine``
    (no ``pg_pool``), so the rejection provably happens before any query.
    """
    for huge in (10**6, 10**9, 10**12):
        monkeypatch.setattr(
            replenishment,
            "load_inventory_reorder_points_config",
            lambda huge=huge: {"consumption_lookback_days": huge, "reorder_points": {"SKU-A": 5}},
        )
        with pytest.raises(ValueError, match="must be at most"):
            await do_recommend_restock(_DummyEngine(), {"namespace_id": uuid.uuid4()})


# ---------------------------------------------------------------------------
# 4. The work-order demand SEAM's default (no DB).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_work_order_demand_default_is_always_zero() -> None:
    result = await _no_work_order_demand(_DummyEngine(), uuid.uuid4(), "SKU-X", None)
    assert result == Decimal("0.000") or result == Decimal("0")


# ---------------------------------------------------------------------------
# Integration helpers — seed directly via the owner pool, matching
# test_inventory_transactions.py's convention. Every helper takes an
# explicit namespace_id and scopes its own SQL by it.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _unique_sku(prefix: str) -> str:
    """A per-run unique SKU.

    The shared integration database is long-lived and accumulates ledger rows
    from every previous run, across dozens of leftover namespaces. A fixed
    literal SKU therefore makes any assertion about "which rows came back"
    depend on that accumulated history instead of on what the test seeded —
    the exact contamination the original cross-namespace test claimed to
    detect and did not. With a unique SKU the only rows in existence for it
    are the ones this test wrote, so a predicate that stops filtering shows up
    as a wrong number rather than as noise."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _seed_location(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    async with pg_pool.acquire() as conn:
        location_id = await conn.fetchval(
            "INSERT INTO stock_locations (namespace_id, kind, name, parent_id, level) "
            "VALUES ($1, 'warehouse', $2, NULL, 0) RETURNING id",
            namespace_id,
            name,
        )
    assert location_id is not None
    return location_id


async def _seed_transaction(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    sku: str,
    location_id: uuid.UUID,
    delta: str,
    reason_category: str,
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Scaffolding-only insert (owner pool, bypasses ``append_transaction``'s
    own validation on purpose) — same convention as
    ``test_inventory_transactions.py``'s helper of the same name.
    ``created_at`` is explicit, not left to ``now()``, so the lookback-window
    exclusion in a test is never a wall-clock-timing assumption."""
    async with pg_pool.acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO inventory_transactions "
            "(namespace_id, sku, location_id, delta, reason_category, created_at) "
            "VALUES ($1, $2, $3, $4, $5, COALESCE($6, now())) RETURNING id",
            namespace_id,
            sku,
            location_id,
            Decimal(delta),
            reason_category,
            created_at,
        )
    assert row_id is not None
    return row_id


def _patch_reorder_points(
    monkeypatch: pytest.MonkeyPatch, reorder_points: dict[str, Any], lookback_days: int = 30
) -> None:
    monkeypatch.setattr(
        replenishment,
        "load_inventory_reorder_points_config",
        lambda: {"consumption_lookback_days": lookback_days, "reorder_points": reorder_points},
    )


class _FakeWorkOrderDemand:
    """Fake WorkOrderDemandSignal — Module 12 (Field Tech) has no schema, so
    this stands in for it (module docstring's SEAM section), never a real
    work-order table.

    RECORDS every ``(ns_uuid, sku, location_id)`` triple it is handed, in call
    order. The Protocol defines that ``location_id`` argument and a real
    Module 12 implementation will branch on it — namespace-wide demand for
    ``None``, that location's demand otherwise. A fake that merely ignores the
    argument therefore cannot tell a correct call from one passing ``None``
    unconditionally, which would hand a location-scoped call the namespace's
    whole demand: over-stated demand, over-ordering at that location, and
    nothing red.
    """

    def __init__(self, qty_by_sku: dict[str, str]) -> None:
        self._qty_by_sku = qty_by_sku
        self.calls: list[tuple[uuid.UUID, str, uuid.UUID | None]] = []

    async def __call__(
        self,
        engine: Any,
        ns_uuid: uuid.UUID,
        sku: str,
        location_id: uuid.UUID | None,
    ) -> Decimal:
        self.calls.append((ns_uuid, sku, location_id))
        return Decimal(self._qty_by_sku.get(sku, "0"))


def _assert_is_work_order_demand_signal(candidate: WorkOrderDemandSignal) -> None:
    """Structural sanity check that the Protocol shape is actually usable as
    a type annotation target — exercised, not just declared."""
    assert callable(candidate)


def test_fake_work_order_demand_satisfies_the_protocol_shape() -> None:
    _assert_is_work_order_demand_signal(_FakeWorkOrderDemand({}))
    _assert_is_work_order_demand_signal(_no_work_order_demand)


# ---------------------------------------------------------------------------
# 5a. Below reorder point -> recommended; above -> not (one call, two SKUs).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recommend_restock_below_reorder_point_is_recommended_above_is_not(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    _patch_reorder_points(monkeypatch, {"SKU-LOW": 5, "SKU-HIGH": 5})

    # SKU-LOW ends up with a ledger balance of 2 — below its reorder point (5).
    await _seed_transaction(
        pg_pool, namespace_id, "SKU-LOW", loc, delta="2.000", reason_category="adjustment"
    )
    # SKU-HIGH ends up with a ledger balance of 10 — above its reorder point (5).
    await _seed_transaction(
        pg_pool, namespace_id, "SKU-HIGH", loc, delta="10.000", reason_category="adjustment"
    )

    engine = _EngineStub(pg_pool)
    result = await do_recommend_restock(engine, {"namespace_id": namespace_id, "location": loc})

    assert result["ok"] is True
    by_sku = {r["sku"]: r for r in result["recommendations"]}
    assert set(by_sku) == {"SKU-LOW", "SKU-HIGH"}

    low = by_sku["SKU-LOW"]
    assert low["current_balance"] == Decimal("2.000")
    assert low["reorder_point"] == Decimal("5.000")
    assert low["recommended"] is True, "a SKU below its reorder point must be recommended"

    high = by_sku["SKU-HIGH"]
    assert high["current_balance"] == Decimal("10.000")
    assert high["reorder_point"] == Decimal("5.000")
    assert high["recommended"] is False, "a SKU above its reorder point must not be recommended"

    # The PER-RECOMMENDATION location_id, not just result["location_id"]. The
    # Returns docstring lists it inside every entry, and a caller that reads
    # entries (a PO builder, a UI row) never sees the envelope — hard-coded to
    # None it would be told each of these lines is namespace-wide when both
    # were computed from loc's rows alone.
    assert low["location_id"] == str(loc)
    assert high["location_id"] == str(loc)


# ---------------------------------------------------------------------------
# 5b. The injected demand signal changes the outcome.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_injected_work_order_demand_flips_the_recommendation(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    _patch_reorder_points(monkeypatch, {"SKU-DEMAND": 5})
    # Ledger balance 10 — above the reorder point (5) with NO demand signal.
    await _seed_transaction(
        pg_pool, namespace_id, "SKU-DEMAND", loc, delta="10.000", reason_category="adjustment"
    )
    engine = _EngineStub(pg_pool)
    params = {"namespace_id": namespace_id, "location": loc, "sku": "SKU-DEMAND"}

    baseline = await do_recommend_restock(engine, params)
    assert baseline["recommendations"][0]["recommended"] is False, (
        "with the default 'no demand' seam, 10 on hand vs a reorder point of 5 must not "
        "be recommended"
    )

    # A fake work-order demand of 8 projects the position down to 10 - 8 = 2,
    # which IS below the reorder point of 5 -- the outcome must flip.
    fake_demand = _FakeWorkOrderDemand({"SKU-DEMAND": "8"})
    with_demand = await do_recommend_restock(engine, params, work_order_demand=fake_demand)
    entry = with_demand["recommendations"][0]
    assert entry["demand_qty"] == Decimal("8.000")
    assert entry["projected_position"] == Decimal("2.000")
    assert entry["recommended"] is True, "the injected demand signal must change the outcome"
    assert fake_demand.calls == [(namespace_id, "SKU-DEMAND", loc)], (
        "the seam must be handed the scope it is being asked to answer for — this call "
        "was location-scoped, so a real Module 12 must not be asked for (and must not "
        "return) the whole namespace's demand"
    )


# ---------------------------------------------------------------------------
# 5c. The rationale names real ledger rows, and excludes out-of-window ones.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rationale_names_real_ledger_rows_and_excludes_out_of_window_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    _patch_reorder_points(monkeypatch, {"SKU-RATIONALE": 5}, lookback_days=30)
    base = datetime.now(timezone.utc)

    in_window_1 = await _seed_transaction(
        pg_pool,
        namespace_id,
        "SKU-RATIONALE",
        loc,
        delta="-3.000",
        reason_category="consumption",
        created_at=base - timedelta(days=1),
    )
    in_window_2 = await _seed_transaction(
        pg_pool,
        namespace_id,
        "SKU-RATIONALE",
        loc,
        delta="-2.000",
        reason_category="transfer_out",
        created_at=base - timedelta(days=2),
    )
    out_of_window = await _seed_transaction(
        pg_pool,
        namespace_id,
        "SKU-RATIONALE",
        loc,
        delta="-999.000",
        reason_category="consumption",
        created_at=base - timedelta(days=60),
    )

    engine = _EngineStub(pg_pool)
    result = await do_recommend_restock(
        engine, {"namespace_id": namespace_id, "location": loc, "sku": "SKU-RATIONALE"}
    )
    entry = result["recommendations"][0]

    assert entry["consumption_velocity_qty"] == Decimal("5.000"), (
        "velocity must total only the two in-window rows (3 + 2), never the out-of-window one"
    )
    cited = {row["id"]: row for row in entry["ledger_rows"]}
    assert set(cited) == {str(in_window_1), str(in_window_2)}
    assert str(out_of_window) not in cited

    # The WHOLE cited row, not just its id. ``ledger_rows`` is the payload a
    # caller (a PO builder, a "why did it say that" UI) reads instead of
    # re-querying the ledger, and every field in it survived being replaced by
    # a constant while this file asserted ids alone. ``created_at`` is seeded
    # EXPLICITLY above, so these are exact round-trips of values this test
    # wrote — not a wall-clock guess.
    assert cited[str(in_window_1)]["delta"] == Decimal("-3.000")
    assert cited[str(in_window_1)]["reason_category"] == "consumption"
    assert datetime.fromisoformat(cited[str(in_window_1)]["created_at"]) == base - timedelta(days=1)
    assert cited[str(in_window_2)]["delta"] == Decimal("-2.000")
    assert cited[str(in_window_2)]["reason_category"] == "transfer_out", (
        "the category is what the rationale uses to explain WHY a row counted "
        "(replenishment.py's depletion-set comment) — mislabelling this internal "
        "transfer as 'consumption' would tell a human stock was used up when it was "
        "moved to another of the tenant's own shelves"
    )
    assert datetime.fromisoformat(cited[str(in_window_2)]["created_at"]) == base - timedelta(days=2)

    assert str(in_window_1) in entry["rationale"], "rationale must name the real ledger rows"
    assert str(in_window_2) in entry["rationale"]
    assert str(out_of_window) not in entry["rationale"], (
        "a row outside the lookback window must not be cited as if it were part of the velocity"
    )


# ---------------------------------------------------------------------------
# 5d. Cross-namespace isolation.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_namespace_ledger_rows_never_influence_the_first(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    make_namespace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``namespace_id`` predicate must be the ONLY thing separating the
    two tenants here.

    Deliberately passes **no** ``location``. ``stock_locations`` ids are
    per-namespace and ``inventory_transactions``' composite FK forbids a
    cross-tenant location reference, so filtering by ns_a's own location id
    already excludes every one of ns_b's rows by itself — with a location
    filter this test stays green even with ``namespace_id = $1`` deleted from
    both query branches, which is exactly how its first version passed while
    proving nothing. Without the location filter the query reduces to
    ``namespace_id = $1 AND sku = $2``: delete the first conjunct and ns_b's
    rows land in ns_a's balance, ns_a's velocity, and ns_a's rationale.

    Both namespaces use the SAME (unique) SKU so ns_b's rows are genuinely
    reachable by the query under test.
    """
    ns_a = namespace_id
    ns_b = await make_namespace()
    loc_a = await _seed_location(pg_pool, ns_a, "Warehouse-A")
    loc_b = await _seed_location(pg_pool, ns_b, "Warehouse-B")
    sku = _unique_sku("SKU-ISO")
    _patch_reorder_points(monkeypatch, {sku: 5})

    # ns_a's own history: 100 stocked, 10 consumed -> balance 90, velocity 10.
    await _seed_transaction(
        pg_pool, ns_a, sku, loc_a, delta="100.000", reason_category="adjustment"
    )
    own_consumption = await _seed_transaction(
        pg_pool, ns_a, sku, loc_a, delta="-10.000", reason_category="consumption"
    )
    # ns_b's history for the SAME sku. Nothing but the namespace_id predicate
    # keeps this row out of the answer below.
    cross_namespace_row = await _seed_transaction(
        pg_pool, ns_b, sku, loc_b, delta="-50.000", reason_category="consumption"
    )

    engine = _EngineStub(pg_pool)
    result = await do_recommend_restock(engine, {"namespace_id": ns_a, "sku": sku})

    assert result["location_id"] is None, (
        "this test is worthless if it is location-filtered — the location filter would "
        "segregate the tenants on its own"
    )
    entry = result["recommendations"][0]

    assert entry["current_balance"] == Decimal("90.000"), (
        "ns_b's -50.000 row must never contribute to ns_a's ledger-derived balance"
    )
    assert entry["consumption_velocity_qty"] == Decimal("10.000"), (
        "ns_b's consumption must never inflate ns_a's velocity"
    )
    cited_ids = {row["id"] for row in entry["ledger_rows"]}
    assert cited_ids == {str(own_consumption)}, "only ns_a's own row may be cited"
    assert str(cross_namespace_row) not in cited_ids
    assert str(cross_namespace_row) not in entry["rationale"], (
        "the cross-namespace row id must be absent from ns_a's rationale"
    )


# ---------------------------------------------------------------------------
# 5e. Documented fallback: a SKU absent from inventory-reorder-points.json.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unconfigured_sku_is_reported_not_recommended_with_null_reorder_point(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    # SKU-UNCONFIGURED is deliberately absent from reorder_points.
    _patch_reorder_points(monkeypatch, {"SKU-OTHER": 5})
    await _seed_transaction(
        pg_pool,
        namespace_id,
        "SKU-UNCONFIGURED",
        loc,
        delta="-1000.000",
        reason_category="consumption",
    )

    engine = _EngineStub(pg_pool)
    result = await do_recommend_restock(
        engine, {"namespace_id": namespace_id, "location": loc, "sku": "SKU-UNCONFIGURED"}
    )
    entry = result["recommendations"][0]

    assert entry["reorder_point"] is None
    assert entry["recommended"] is False, "an unconfigured SKU must never be guessed as recommended"
    assert "no reorder point configured" in entry["rationale"]


# ---------------------------------------------------------------------------
# 5f. Omitting "sku" evaluates exactly the configured SKU set; no config ->
# no recommendations (still opens a scoped session -- proves it doesn't error).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_configured_reorder_points_yields_no_recommendations(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_reorder_points(monkeypatch, {})
    engine = _EngineStub(pg_pool)
    result = await do_recommend_restock(engine, {"namespace_id": namespace_id})
    assert result["ok"] is True
    assert result["recommendations"] == []


# ---------------------------------------------------------------------------
# 5g. The DEFAULT call shape (no "location") against real rows, and the proof
#     that the location predicate actually filters.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_omitting_location_sums_every_location_and_supplying_one_does_not(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented default — ``location`` omitted means every location —
    exercised against REAL rows in more than one location.

    This is the branch where ``namespace_id`` is the SOLE isolation predicate,
    and (before this test) nothing in the suite ever reached its loop body:
    every other integration test passed a ``location``, and the one that
    omitted it configured zero SKUs.

    It is also the test that pins the ``location_id`` predicate itself. The
    namespace-wide total (40) and the per-location totals (30 / 10) are
    deliberately three different numbers, so dropping ``AND location_id = $3``
    from the location-scoped query makes both per-location assertions fail
    instead of quietly returning the same answer.
    """
    loc_a = await _seed_location(pg_pool, namespace_id, "Warehouse-A")
    loc_b = await _seed_location(pg_pool, namespace_id, "Warehouse-B")
    sku = _unique_sku("SKU-ALLLOC")
    _patch_reorder_points(monkeypatch, {sku: 5})

    await _seed_transaction(
        pg_pool, namespace_id, sku, loc_a, delta="30.000", reason_category="adjustment"
    )
    await _seed_transaction(
        pg_pool, namespace_id, sku, loc_b, delta="12.000", reason_category="adjustment"
    )
    at_b = await _seed_transaction(
        pg_pool, namespace_id, sku, loc_b, delta="-2.000", reason_category="consumption"
    )

    engine = _EngineStub(pg_pool)

    every_location = await do_recommend_restock(engine, {"namespace_id": namespace_id})
    assert every_location["location_id"] is None
    ns_entry = every_location["recommendations"][0]
    assert ns_entry["location_id"] is None, "a namespace-wide entry must say so per-entry too"
    assert ns_entry["sku"] == sku, "omitting 'sku' evaluates exactly the configured SKU set"
    assert ns_entry["current_balance"] == Decimal("40.000"), (
        "30 at loc_a + 12 at loc_b - 2 consumed at loc_b — every location in the namespace"
    )
    assert ns_entry["consumption_velocity_qty"] == Decimal("2.000")
    assert {row["id"] for row in ns_entry["ledger_rows"]} == {str(at_b)}
    assert ns_entry["recommended"] is False

    only_a = await do_recommend_restock(engine, {"namespace_id": namespace_id, "location": loc_a})
    assert only_a["recommendations"][0]["current_balance"] == Decimal("30.000"), (
        "a location-scoped call must see loc_a's rows ONLY, never the namespace total"
    )
    assert only_a["recommendations"][0]["consumption_velocity_qty"] == Decimal("0.000")
    assert only_a["recommendations"][0]["ledger_rows"] == []
    # Same three call shapes, now read PER ENTRY: None / loc_a / loc_b are
    # three different values, so an entry hard-coded to None (or to the wrong
    # location) cannot pass all three.
    assert only_a["recommendations"][0]["location_id"] == str(loc_a)

    only_b = await do_recommend_restock(engine, {"namespace_id": namespace_id, "location": loc_b})
    assert only_b["recommendations"][0]["current_balance"] == Decimal("10.000")
    assert {row["id"] for row in only_b["recommendations"][0]["ledger_rows"]} == {str(at_b)}
    assert only_b["recommendations"][0]["location_id"] == str(loc_b)


# ---------------------------------------------------------------------------
# 5h. Velocity's depletion set is scope-dependent: an internal transfer is
#     demand at a location, but not across the namespace.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_internal_transfer_counts_as_depletion_per_location_but_not_namespace_wide(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Move 40 from A to B (a real ``transfer_out``/``transfer_in`` pair) and
    consume 10 at A.

    Location-scoped at A: 40 genuinely left A and must be replenished AT A, so
    the ``transfer_out`` counts — velocity 50.

    Namespace-wide: the pair nets to zero inside the scope, nothing was
    consumed by the transfer, so only the consumption row counts — velocity
    10. Counting the ``transfer_out`` half here (the pre-fix behaviour) would
    report 40 units of demand that do not exist, and cite an internal-movement
    row to a human as consumption.
    """
    loc_a = await _seed_location(pg_pool, namespace_id, "Warehouse-A")
    loc_b = await _seed_location(pg_pool, namespace_id, "Warehouse-B")
    sku = _unique_sku("SKU-XFER")
    _patch_reorder_points(monkeypatch, {sku: 5})

    await _seed_transaction(
        pg_pool, namespace_id, sku, loc_a, delta="100.000", reason_category="adjustment"
    )
    transfer_out = await _seed_transaction(
        pg_pool, namespace_id, sku, loc_a, delta="-40.000", reason_category="transfer_out"
    )
    await _seed_transaction(
        pg_pool, namespace_id, sku, loc_b, delta="40.000", reason_category="transfer_in"
    )
    consumed_at_a = await _seed_transaction(
        pg_pool, namespace_id, sku, loc_a, delta="-10.000", reason_category="consumption"
    )

    engine = _EngineStub(pg_pool)

    namespace_wide = (
        await do_recommend_restock(engine, {"namespace_id": namespace_id, "sku": sku})
    )["recommendations"][0]
    assert namespace_wide["current_balance"] == Decimal("90.000"), (
        "100 in, 40 moved out of A and back in at B (nets to zero), 10 consumed"
    )
    assert namespace_wide["consumption_velocity_qty"] == Decimal("10.000"), (
        "namespace-wide, an internal transfer is not consumption — only the 10 consumed"
    )
    assert {row["id"] for row in namespace_wide["ledger_rows"]} == {str(consumed_at_a)}
    assert str(transfer_out) not in namespace_wide["rationale"], (
        "the transfer_out row must not be cited to a human as namespace-wide demand"
    )

    at_location_a = (
        await do_recommend_restock(
            engine, {"namespace_id": namespace_id, "location": loc_a, "sku": sku}
        )
    )["recommendations"][0]
    assert at_location_a["current_balance"] == Decimal("50.000")
    assert at_location_a["consumption_velocity_qty"] == Decimal("50.000"), (
        "at loc_a the transfer_out DID deplete the shelf — 40 transferred + 10 consumed"
    )
    cited_at_a = {row["id"]: row for row in at_location_a["ledger_rows"]}
    assert set(cited_at_a) == {str(transfer_out), str(consumed_at_a)}
    # The categories are the whole subject of this test, so assert them in the
    # payload the caller actually reads. 80% of the 50.000 came from the
    # transfer row; labelled 'consumption' in ``ledger_rows`` it would read to
    # a human as stock used up rather than stock relocated — the exact
    # confusion this test's namespace-wide half exists to prevent.
    assert cited_at_a[str(transfer_out)]["delta"] == Decimal("-40.000")
    assert cited_at_a[str(transfer_out)]["reason_category"] == "transfer_out"
    assert cited_at_a[str(consumed_at_a)]["delta"] == Decimal("-10.000")
    assert cited_at_a[str(consumed_at_a)]["reason_category"] == "consumption"


# ---------------------------------------------------------------------------
# 5i. An unknown / foreign location is a caller error, not an empty warehouse.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_location_uuid_is_rejected_not_reported_as_a_phantom_zero(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale or mistyped location id matches no ledger row, and an empty
    ledger read is indistinguishable from a genuinely empty location — so
    without validation the advisor answers ``current_balance 0.000`` /
    ``recommended: true`` with a confident "BELOW the configured reorder
    point" rationale for every configured SKU. "Restock everything" must not
    be reachable by a typo.

    Configures a SKU with a real reorder point so the failure mode this
    replaces (a phantom recommendation) would actually be produced.
    """
    sku = _unique_sku("SKU-PHANTOM")
    _patch_reorder_points(monkeypatch, {sku: 5})
    engine = _EngineStub(pg_pool)
    stale_location = uuid.uuid4()

    with pytest.raises(ValueError, match="is not a stock_locations id in namespace"):
        await do_recommend_restock(
            engine, {"namespace_id": namespace_id, "location": stale_location}
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_another_tenants_location_is_rejected_even_with_no_skus_configured(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    make_namespace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another tenant's location id is a real ``stock_locations`` row — just
    not one of THIS namespace's — so the validation must bind ``namespace_id``,
    not merely check existence.

    Uses an EMPTY reorder-point map on purpose: the guard runs before the
    per-SKU loop, so it must fire even when there is no SKU to evaluate. With
    the guard placed inside the loop this call would return ``ok: True`` and
    an empty recommendation list instead of raising.
    """
    ns_b = await make_namespace()
    foreign_location = await _seed_location(pg_pool, ns_b, "Warehouse-B")
    _patch_reorder_points(monkeypatch, {})
    engine = _EngineStub(pg_pool)

    with pytest.raises(ValueError, match="is not a stock_locations id in namespace"):
        await do_recommend_restock(
            engine, {"namespace_id": namespace_id, "location": foreign_location}
        )

    # …and the tenant that DOES own it is unaffected.
    ok = await do_recommend_restock(engine, {"namespace_id": ns_b, "location": foreign_location})
    assert ok["ok"] is True
    assert ok["location_id"] == str(foreign_location)


# ---------------------------------------------------------------------------
# 5j. The negative-demand clamp is load-bearing, not decoration.
# ---------------------------------------------------------------------------


class _MisbehavingWorkOrderDemand:
    """A seam that violates the Protocol's "non-negative Decimal" contract."""

    async def __call__(
        self,
        engine: Any,
        ns_uuid: uuid.UUID,
        sku: str,
        location_id: uuid.UUID | None,
    ) -> Decimal:
        return Decimal("-100.000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_negative_demand_from_a_misbehaving_seam_is_clamped_and_cannot_suppress_restock(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``projected_position = current_balance - demand_qty``, so a NEGATIVE
    demand ADDS to the projected position.

    Balance 3 against a reorder point of 5 must be recommended. Without the
    clamp, a seam returning -100 projects the position to 103, silently
    suppresses the restock, and reports ``demand_qty: -100.000`` — a
    misbehaving injected implementation quietly turning the advisor off. The
    clamp is the only thing standing between those two outcomes, and this test
    asserts all three consequences of it.
    """
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = _unique_sku("SKU-CLAMP")
    _patch_reorder_points(monkeypatch, {sku: 5})
    await _seed_transaction(
        pg_pool, namespace_id, sku, loc, delta="3.000", reason_category="adjustment"
    )

    engine = _EngineStub(pg_pool)
    entry = (
        await do_recommend_restock(
            engine,
            {"namespace_id": namespace_id, "location": loc, "sku": sku},
            work_order_demand=_MisbehavingWorkOrderDemand(),
        )
    )["recommendations"][0]

    assert entry["demand_qty"] == Decimal("0.000"), "a negative demand must be clamped to zero"
    assert entry["projected_position"] == Decimal("3.000"), (
        "an unclamped -100 would inflate the projected position to 103.000"
    )
    assert entry["recommended"] is True, (
        "3 on hand against a reorder point of 5 must still be recommended — a misbehaving "
        "seam must not be able to suppress a restock"
    )


# ---------------------------------------------------------------------------
# 5k. Shrinkage is namespace-wide demand: a NEGATIVE adjustment has no
#     counterpart row to net against.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_negative_adjustment_shrinkage_counts_as_namespace_wide_demand(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeds a REAL negative ``adjustment`` — shrinkage, breakage, a
    cycle-count write-down — and pins the namespace-wide velocity number and
    the rows cited for it.

    This is the DEFAULT call shape (no ``location``). 051's sign CHECK leaves
    ``adjustment`` unconstrained in sign, so it is the only category that can
    carry a negative delta while being neither a transfer nor a consumption,
    and — unlike a ``transfer_out`` — it has no counterpart row anywhere in
    the namespace to net against. The stock is simply gone.

    With the namespace-wide set narrowed to ``{"consumption"}`` this call
    answers ``10.000`` and never cites the write-off row, under-stating demand
    and under-recommending restock precisely where stock is quietly
    disappearing. Both scopes are asserted because they must AGREE about a
    write-down (they differ only about ``transfer_out``) — the narrowed set
    made them disagree, 10.000 namespace-wide against 40.000 at the location,
    for the very same three rows.
    """
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = _unique_sku("SKU-SHRINK")
    _patch_reorder_points(monkeypatch, {sku: 5})

    await _seed_transaction(
        pg_pool, namespace_id, sku, loc, delta="100.000", reason_category="adjustment"
    )
    write_off = await _seed_transaction(
        pg_pool, namespace_id, sku, loc, delta="-30.000", reason_category="adjustment"
    )
    consumed = await _seed_transaction(
        pg_pool, namespace_id, sku, loc, delta="-10.000", reason_category="consumption"
    )

    engine = _EngineStub(pg_pool)

    namespace_wide = (
        await do_recommend_restock(engine, {"namespace_id": namespace_id, "sku": sku})
    )["recommendations"][0]

    assert namespace_wide["location_id"] is None, "5k is worthless if it is location-scoped"
    assert namespace_wide["current_balance"] == Decimal("60.000"), "100 in, 30 lost, 10 consumed"
    assert namespace_wide["consumption_velocity_qty"] == Decimal("40.000"), (
        "30 written off + 10 consumed — the write-off depleted the NAMESPACE, not just a "
        "shelf, and has no transfer_in half to net it out"
    )
    cited = {row["id"]: row for row in namespace_wide["ledger_rows"]}
    assert set(cited) == {str(write_off), str(consumed)}
    # The cited PAYLOAD, not just the ids. This test exists to distinguish a
    # write-down from a consumption, and the row it cites is the only thing
    # telling a human which of the two the 40.000 came from — relabel the
    # shrinkage row 'consumption' in the payload and every id-only assertion
    # above stays green while the explanation becomes false.
    assert cited[str(write_off)]["delta"] == Decimal("-30.000")
    assert cited[str(write_off)]["reason_category"] == "adjustment", (
        "a shrinkage/breakage/cycle-count write-down is an ADJUSTMENT row, and the "
        "rationale surfaces that category precisely to explain why it counted"
    )
    assert cited[str(consumed)]["delta"] == Decimal("-10.000")
    assert cited[str(consumed)]["reason_category"] == "consumption"
    assert str(write_off) in namespace_wide["rationale"], (
        "the human asking 'why did it say that' must be shown the shrinkage row that "
        "drove three quarters of the demand figure"
    )

    at_location = (
        await do_recommend_restock(
            engine, {"namespace_id": namespace_id, "location": loc, "sku": sku}
        )
    )["recommendations"][0]
    assert at_location["consumption_velocity_qty"] == Decimal("40.000"), (
        "the two scopes must agree about a write-down — transfer_out is the only category "
        "they are allowed to disagree about"
    )
    assert {row["id"] for row in at_location["ledger_rows"]} == {str(write_off), str(consumed)}


# ---------------------------------------------------------------------------
# 5l. The reorder trigger's own boundary: projected_position == reorder_point.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_projected_position_exactly_at_the_reorder_point_is_not_recommended(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one comparison this whole module exists to make, tested ON its
    boundary — every other balance in this file (2, 3, 10, 40, 90) is strictly
    below or strictly above a reorder point of 5, so ``<`` and ``<=`` are
    indistinguishable to them.

    A reorder point is a floor you restock BELOW, not AT: sitting exactly on
    it is "at/above". Under ``<=`` this call not only flips ``recommended`` to
    True, it emits a literally false sentence to a human — "= 5.000 projected
    position, BELOW the configured reorder point of 5.000" — which is why the
    rationale wording is asserted here and not just the boolean.

    The second half moves the position one thousandth (the ledger's own
    scale) below the point and asserts the outcome DOES flip, so this test
    cannot pass by answering False to everything.
    """
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = _unique_sku("SKU-BOUNDARY")
    _patch_reorder_points(monkeypatch, {sku: 5})
    await _seed_transaction(
        pg_pool, namespace_id, sku, loc, delta="5.000", reason_category="adjustment"
    )

    engine = _EngineStub(pg_pool)
    params = {"namespace_id": namespace_id, "location": loc, "sku": sku}

    at_the_point = (await do_recommend_restock(engine, params))["recommendations"][0]
    assert at_the_point["projected_position"] == at_the_point["reorder_point"] == Decimal("5.000")
    assert at_the_point["recommended"] is False, (
        "a SKU sitting exactly ON its reorder point is not below it — '<' not '<='"
    )
    assert "at/above the configured reorder point" in at_the_point["rationale"]
    assert "BELOW" not in at_the_point["rationale"], (
        "under '<=' the rationale reads '= 5.000 projected position, BELOW the configured "
        "reorder point of 5.000' — a false sentence in the human-facing explanation"
    )

    # One thousandth below the point — the smallest step the ledger's own
    # NUMERIC(18,3) scale can express — and the outcome must flip.
    one_thousandth_below = (
        await do_recommend_restock(
            engine, params, work_order_demand=_FakeWorkOrderDemand({sku: "0.001"})
        )
    )["recommendations"][0]
    assert one_thousandth_below["projected_position"] == Decimal("4.999")
    assert one_thousandth_below["recommended"] is True, (
        "strictly below the reorder point must be recommended — otherwise this test "
        "would pass against code that never recommends anything"
    )
    assert "BELOW the configured reorder point" in one_thousandth_below["rationale"]

    # And the same boundary is where the demand seam's own quantise
    # (``do_recommend_restock``: ``_quantise_qty(_as_decimal(demand_qty, ...))``)
    # becomes load-bearing rather than cosmetic. A seam returning 0.0004 is
    # returning a quantity the ledger's NUMERIC(18,3) scale cannot express;
    # rounded half-up it is 0.000 and the position stays exactly ON the point.
    # WITHOUT the quantise it is carried at full precision, projected_position
    # is 4.9996 — strictly below 5.000 — and this SKU is recommended for
    # restock off four ten-thousandths of a unit of phantom demand. Every
    # other demand figure in this file (0, 8, 0.001, -100) is already at or
    # coarser than 3dp, so the quantise is invisible to all of them.
    sub_thousandth_demand = (
        await do_recommend_restock(
            engine, params, work_order_demand=_FakeWorkOrderDemand({sku: "0.0004"})
        )
    )["recommendations"][0]
    assert sub_thousandth_demand["demand_qty"] == Decimal("0.000"), (
        "a sub-thousandth demand must be quantised to the ledger's own 3dp scale, not "
        "reported back at a precision the ledger cannot hold"
    )
    assert sub_thousandth_demand["projected_position"] == Decimal("5.000"), (
        "unquantised, 5.000 - 0.0004 leaves the position at 4.9996 instead of 5.000"
    )
    assert sub_thousandth_demand["recommended"] is False, (
        "0.0004 of demand must not be able to push a SKU sitting exactly on its reorder "
        "point over the line"
    )


# ---------------------------------------------------------------------------
# 5m. The demand seam is told WHICH scope it must answer for.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demand_seam_receives_the_location_scope_of_the_call(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``work_order_demand(engine, ns_uuid, sku, location_id)`` — the
    ``location_id`` argument is part of the Protocol's contract, and until now
    nothing asserted what was actually passed.

    Module 12 does not exist, so the only place this contract can be checked
    is here. A real implementation branches on it: namespace-wide demand for
    ``None``, that location's demand otherwise. Pass ``None`` unconditionally
    and a location-scoped call is quoted the WHOLE namespace's upcoming
    demand, deflating projected_position and over-ordering at that location —
    with every existing assertion still green, because the fake ignored the
    argument.

    Both scopes are exercised with the same fake, in one test, so the recorded
    calls are two DIFFERENT values (``loc`` then ``None``): neither a
    hard-coded ``None`` nor a hard-coded ``loc`` can satisfy both.
    """
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = _unique_sku("SKU-SEAM")
    _patch_reorder_points(monkeypatch, {sku: 5})
    await _seed_transaction(
        pg_pool, namespace_id, sku, loc, delta="10.000", reason_category="adjustment"
    )

    engine = _EngineStub(pg_pool)
    recorder = _FakeWorkOrderDemand({})

    await do_recommend_restock(
        engine,
        {"namespace_id": namespace_id, "location": loc, "sku": sku},
        work_order_demand=recorder,
    )
    await do_recommend_restock(
        engine, {"namespace_id": namespace_id, "sku": sku}, work_order_demand=recorder
    )

    assert recorder.calls == [
        (namespace_id, sku, loc),
        (namespace_id, sku, None),
    ], (
        "the seam must see the same (namespace, sku, scope) the recommendation is being "
        "computed for — location-scoped first, namespace-wide second"
    )


# ---------------------------------------------------------------------------
# 5n. The CONFIGURED lookback window is the one actually applied — and the one
#     reported back.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_configured_lookback_window_is_applied_and_reported_back(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``consumption_lookback_days`` at values that are NOT 30.

    Every other test in this file that reaches the window uses 30 — which is
    ``_DEFAULT_LOOKBACK_DAYS`` — so all of them pass identically against code
    that reads the config and against code that ignores it and hard-codes 30.
    The module docstring and the config file's own ``_comment`` argue at
    length that "0 and 30 are different instructions"; until this test nothing
    proved that 7 and 30 are, and nothing anywhere asserted the
    ``consumption_lookback_days`` the response reports back.

    Three assertions, at two NON-default windows, over one fixed set of rows:

    * the velocity FIGURE (3.000 at 7 days, 114.000 at 365) — pins the cutoff
      that ``_compute_ledger_metrics`` is handed;
    * the CITED-ROW COUNT (1 vs 3) — pins which rows the human is shown, not
      just the total;
    * ``result["consumption_lookback_days"]`` (7, then 365) — pins the
      envelope field, which is a separate literal and can be hard-coded on its
      own.

    Two windows rather than one, because a single one cannot tell "reads the
    config" from "hard-codes THIS value": 7, 30 (every other test) and 365 are
    three different answers over the same three depleting rows.
    """
    loc = await _seed_location(pg_pool, namespace_id, "Warehouse")
    sku = _unique_sku("SKU-LOOKBACK")
    base = datetime.now(timezone.utc)

    # created_at is EXPLICIT on every row, so which side of a cutoff a row
    # falls on is never a wall-clock-timing assumption.
    await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="200.000",
        reason_category="adjustment",
        created_at=base - timedelta(days=300),
    )
    within_7 = await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="-3.000",
        reason_category="consumption",
        created_at=base - timedelta(days=2),
    )
    # The two BOUNDARY rows. Without these the test only BOUNDS the cutoff —
    # rows at 2/20/200 days are satisfied by any effective window in (2, 20],
    # so `days + 1`, `days - 1`, `days * 2`, `min(days, 400)` and flipping
    # `created_at >= cutoff` to `>` all read the config, transform it, and stay
    # green. Straddling 7 days by half a day each way PINS it: the day-6.5 row
    # must be IN and the day-7.5 row must be OUT, and half-days keep both off
    # the equality boundary so neither depends on `>=` vs `>`.
    just_inside_7 = await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="-1.000",
        reason_category="consumption",
        created_at=base - timedelta(days=6, hours=12),
    )
    just_outside_7 = await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="-2.000",
        reason_category="consumption",
        created_at=base - timedelta(days=7, hours=12),
    )
    outside_7_within_30 = await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="-11.000",
        reason_category="consumption",
        created_at=base - timedelta(days=20),
    )
    outside_30_within_365 = await _seed_transaction(
        pg_pool,
        namespace_id,
        sku,
        loc,
        delta="-100.000",
        reason_category="consumption",
        created_at=base - timedelta(days=200),
    )

    engine = _EngineStub(pg_pool)
    params = {"namespace_id": namespace_id, "location": loc, "sku": sku}

    _patch_reorder_points(monkeypatch, {sku: 5}, lookback_days=7)
    seven = await do_recommend_restock(engine, params)

    assert seven["consumption_lookback_days"] == 7, (
        "the envelope must report the window the config asked for, not the 30-day "
        "default — a caller reconciling this answer against its own config has nothing "
        "else to check it with"
    )
    assert seven["consumption_lookback_days"] != _DEFAULT_LOOKBACK_DAYS
    seven_entry = seven["recommendations"][0]
    assert seven_entry["consumption_velocity_qty"] == Decimal("4.000"), (
        "at 7 days the day-2 and day-6.5 rows are inside and the day-7.5 row is "
        "OUT — a window of 8 answers 6.000 here, a window of 6 answers 3.000, a "
        "30-day cutoff 17.000 and a 365-day one 117.000. Measured: `days + 1`, "
        "`days - 1` and `days * 2` each turn this RED, where before the boundary "
        "rows existed all three stayed green. NOT pinned against a clamp above the "
        "tested range (`min(days, 400)` is a no-op at 7 and 365) — a named residue, "
        "not a claim of completeness"
    )
    assert len(seven_entry["ledger_rows"]) == 2, (
        "two cited rows at 7 days — the count is asserted separately from the total "
        "because a wrong window changes WHICH rows a human is shown, not only the sum"
    )
    assert {row["id"] for row in seven_entry["ledger_rows"]} == {
        str(within_7),
        str(just_inside_7),
    }
    assert str(just_outside_7) not in seven_entry["rationale"], (
        "the day-7.5 row is half a day outside a 7-day window; citing it would mean "
        "the cutoff is off by a day in the direction that silently inflates velocity"
    )
    assert str(outside_7_within_30) not in seven_entry["rationale"], (
        "a row 20 days old is outside a 7-day window and must not be cited as if it "
        "were part of the velocity"
    )
    assert "7d consumption velocity" in seven_entry["rationale"], (
        "the human-facing sentence must name the window it actually used"
    )
    # The balance is NOT windowed — it sums every row ever, including the
    # 300-day-old stocking row and the 200-day-old consumption.
    assert seven_entry["current_balance"] == Decimal("83.000")

    _patch_reorder_points(monkeypatch, {sku: 5}, lookback_days=365)
    year = await do_recommend_restock(engine, params)

    assert year["consumption_lookback_days"] == 365
    year_entry = year["recommendations"][0]
    assert year_entry["consumption_velocity_qty"] == Decimal("117.000"), (
        "at 365 days all five depleting rows are inside the window (3 + 1 + 2 + 11 + 100)"
    )
    assert len(year_entry["ledger_rows"]) == 5
    assert {row["id"] for row in year_entry["ledger_rows"]} == {
        str(within_7),
        str(just_inside_7),
        str(just_outside_7),
        str(outside_7_within_30),
        str(outside_30_within_365),
    }
    assert year_entry["current_balance"] == seven_entry["current_balance"], (
        "widening the lookback must move the velocity, never the ledger-derived stock "
        "position — that is the figure the reorder comparison is made against"
    )
