"""
tests/unit/test_economy_ngaap.py
=================================
Acceptance tests for Batch 117 — Module 8.Wave 2 (ngaap-buckets).

Split the same three ways as ``test_economy_match.py``:
  (a) ALGORITHM tests — ported from Andreas's ``tests/finance/cost-engine.test.ts``, run
      against a ``_FIXTURE_CHART``/``_FIXTURE_MAPPING`` defined in THIS file with deliberately
      fake account numbers, never the tenant's real JSON. The accrual arithmetic must be provable
      without touching production config — that is the whole claim of round-2 #5.
  (b) WAVE tests — this wave's own required cases: the seven buckets sum to the input total
      exactly; a cost straddling the period boundary periodises into the correct
      accrued/deferred/WIP split and does so as a REAL ACCRUAL rather than a flat pro-rata
      allocation; swapping the chart JSON changes the target accounts but not one øre of the
      split; the coercion boundary fails loud.
  (c) CONFIG tests — the two real JSON files parse and carry the documented keys/accounts.

Jurisdiction note (round-2 hardening #5): every case below is **Norwegian GAAP**
(regnskapsloven §4-1). There is no IFRS/US-GAAP case here and none should be added — a second
jurisdiction is an engine extension, not a config swap, and is explicitly future work. A test
that branches on a non-Norwegian GAAP rule is out of scope for this wave.

All tests are plain unit tests — no DB, no HTTP, no ``@pytest.mark.integration``.
"""

from __future__ import annotations

import copy
import decimal
from decimal import Decimal

import pytest

from nce.vertical_modules.economy.ngaap import (
    ALL_BUCKETS,
    _product,
    do_compute_bucket_targets,
    load_finago_account_mapping,
    load_finago_chart_of_accounts,
)

# ---------------------------------------------------------------------------
# Fixture config — fake account numbers on purpose. The algorithm tests must be immune to a
# change in the tenant's real chart, and the config-swap test needs a chart it can vary freely.
# ---------------------------------------------------------------------------

_FIXTURE_BUCKET_ACCOUNTS = {
    "hardware": {"cogs": "9300", "revenue": "9000", "accrued": "9531", "deferred": "9901"},
    "materials": {"cogs": "9303", "revenue": "9003", "accrued": "9532", "deferred": "9902"},
    "freight": {"cogs": "9060", "revenue": "9520", "accrued": "9533", "deferred": "9903"},
    "pm": {"cogs": "9500", "revenue": "9016", "accrued": "9534", "deferred": "9904"},
    "tek": {"cogs": "9500", "revenue": "9015", "accrued": "9539", "deferred": "9905"},
    "programming": {"cogs": "9500", "revenue": "9014", "accrued": "9536", "deferred": "9906"},
    "travel": {"cogs": "9160", "revenue": "9013", "accrued": "9538", "deferred": "9908"},
}

_FIXTURE_SHARED_ACCOUNTS = {"wip": "9771"}


def _plan_for(bucket_accounts: dict, shared_accounts: dict) -> dict:
    """Build the ``accounts`` plan covering exactly the referenced numbers."""
    numbers = {account for roles in bucket_accounts.values() for account in roles.values()} | set(
        shared_accounts.values()
    )
    return {number: {"name": f"Fixture account {number}", "type": "asset"} for number in numbers}


_FIXTURE_CHART: dict = {
    "country": "NO",
    "gaap": "NGAAP",
    "buckets": list(ALL_BUCKETS),
    "bucket_accounts": _FIXTURE_BUCKET_ACCOUNTS,
    "shared_accounts": _FIXTURE_SHARED_ACCOUNTS,
    "accounts": _plan_for(_FIXTURE_BUCKET_ACCOUNTS, _FIXTURE_SHARED_ACCOUNTS),
}

_FIXTURE_MAPPING: dict = {
    "roles": ["cogs", "revenue", "accrued", "deferred", "wip"],
    "role_mva_code": {"cogs": 0, "revenue": 3, "accrued": 0, "deferred": 0, "wip": 0},
    "role_balance_side": {
        "cogs": "debit",
        "revenue": "credit",
        "accrued": "debit",
        "deferred": "credit",
        "wip": "debit",
    },
    "account_mva_overrides": {},
}


def _swapped_chart() -> dict:
    """A second, structurally identical chart with every account number different — a tenant
    re-mapping the plan to its own country's numbering."""
    chart = copy.deepcopy(_FIXTURE_CHART)
    bucket_accounts = {
        bucket: {role: f"7{account[1:]}" for role, account in roles.items()}
        for bucket, roles in _FIXTURE_BUCKET_ACCOUNTS.items()
    }
    shared_accounts = {"wip": "7771"}
    chart["bucket_accounts"] = bucket_accounts
    chart["shared_accounts"] = shared_accounts
    chart["accounts"] = _plan_for(bucket_accounts, shared_accounts)
    return chart


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AMOUNT_KEYS = (
    "target_accrued",
    "target_deferred",
    "target_recognized_cogs",
    "target_wip",
    "target_unrecognized",
    "earned_revenue",
    "actual_cost",
    "actual_invoiced",
    "recognition_basis_pct",
)


def _run(
    *,
    bucket: str = "hardware",
    chart: dict | None = None,
    mapping: dict | None = None,
    **inputs: object,
) -> dict:
    """Periodise one bucket and return that bucket's result entry.

    Mirrors Andreas's ``makeInput(overrides)`` fixture: every field defaults to 0/False, and a
    keyword argument overrides exactly that field.
    """
    result = do_compute_bucket_targets(
        chart if chart is not None else _FIXTURE_CHART,
        mapping if mapping is not None else _FIXTURE_MAPPING,
        {"buckets": {bucket: dict(inputs)}},
    )
    return next(entry for entry in result["buckets"] if entry["bucket"] == bucket)


def _amounts(entry: dict) -> dict:
    return {key: entry[key] for key in _AMOUNT_KEYS}


def _split_of(result: dict) -> list[dict]:
    """Every computed amount, per bucket, with the account resolution stripped out — i.e. the
    part that must NOT change when config changes."""
    return [_amounts(entry) | {"bucket": entry["bucket"]} for entry in result["buckets"]]


def _accounts_of(result: dict) -> list[dict]:
    return [
        {role: target["account"] for role, target in entry["accounts"].items()}
        for entry in result["buckets"]
    ]


# ===========================================================================
# (a) ALGORITHM — ported from cost-engine.test.ts
# ===========================================================================


class TestRevenueSide:
    """``describe('computeBucketTargets — revenue-side')``."""

    def test_50pct_delivered_0_invoiced_accrues_50pct(self) -> None:
        entry = _run(expected_revenue=100_000, delivery_pct=0.5, actual_invoiced=0)
        assert entry["target_accrued"] == Decimal("50000.00")
        assert entry["target_deferred"] == Decimal("0.00")
        assert entry["earned_revenue"] == Decimal("50000.00")

    def test_50pct_delivered_100pct_invoiced_defers_50pct(self) -> None:
        entry = _run(expected_revenue=100_000, delivery_pct=0.5, actual_invoiced=100_000)
        assert entry["target_accrued"] == Decimal("0.00")
        assert entry["target_deferred"] == Decimal("50000.00")

    def test_100pct_delivered_100pct_invoiced_is_balanced(self) -> None:
        entry = _run(expected_revenue=100_000, delivery_pct=1.0, actual_invoiced=100_000)
        assert entry["target_accrued"] == Decimal("0.00")
        assert entry["target_deferred"] == Decimal("0.00")

    def test_co_gated_excludes_co_revenue_from_earned(self) -> None:
        # 100k original + 50k change order the customer has NOT approved (A.7).
        entry = _run(
            expected_revenue=150_000,
            expected_revenue_from_co=50_000,
            delivery_pct=1.0,
            actual_invoiced=0,
            co_recognition_gated=True,
        )
        assert entry["earned_revenue"] == Decimal("100000.00")
        assert entry["target_accrued"] == Decimal("100000.00")
        assert entry["target_unrecognized"] == Decimal("50000.00")

    def test_co_approved_includes_whole_revenue_in_earned(self) -> None:
        entry = _run(
            expected_revenue=150_000,
            expected_revenue_from_co=50_000,
            delivery_pct=1.0,
            actual_invoiced=0,
            co_recognition_gated=False,
        )
        assert entry["earned_revenue"] == Decimal("150000.00")
        assert entry["target_accrued"] == Decimal("150000.00")
        assert entry["target_unrecognized"] == Decimal("0.00")


class TestCostSide:
    """``describe('computeBucketTargets — cost-side')``."""

    def test_50pct_delivery_recognizes_half_the_expected_cost(self) -> None:
        entry = _run(expected_cost=80_000, actual_cost=40_000, delivery_pct=0.5)
        assert entry["target_recognized_cogs"] == Decimal("40000.00")
        assert entry["target_wip"] == Decimal("0.00")

    def test_cost_spent_faster_than_delivered_gives_positive_wip(self) -> None:
        entry = _run(expected_cost=80_000, actual_cost=50_000, delivery_pct=0.5)
        assert entry["target_recognized_cogs"] == Decimal("40000.00")
        assert entry["target_wip"] == Decimal("10000.00")

    def test_cost_below_expectation_at_delivery_gives_negative_wip(self) -> None:
        entry = _run(expected_cost=80_000, actual_cost=30_000, delivery_pct=0.5)
        assert entry["target_recognized_cogs"] == Decimal("40000.00")
        assert entry["target_wip"] == Decimal("-10000.00")


class TestEdgeCases:
    """``describe('computeBucketTargets — edge cases')``."""

    def test_zero_delivery_earns_and_recognizes_nothing(self) -> None:
        entry = _run(expected_revenue=100_000, expected_cost=80_000, delivery_pct=0)
        assert entry["earned_revenue"] == Decimal("0.00")
        assert entry["target_recognized_cogs"] == Decimal("0.00")
        assert entry["target_accrued"] == Decimal("0.00")

    def test_over_delivery_is_not_capped_here(self) -> None:
        # Reference: "Engine-en capper IKKE — delivery% må være legit (cap er i input-laget)."
        entry = _run(expected_revenue=100_000, delivery_pct=1.05, actual_invoiced=100_000)
        assert entry["earned_revenue"] == Decimal("105000.00")
        assert entry["target_accrued"] == Decimal("5000.00")

    def test_recognition_basis_pct_is_delivery_times_100(self) -> None:
        entry = _run(expected_revenue=100_000, delivery_pct=0.5)
        assert entry["recognition_basis_pct"] == Decimal("50")

    def test_negative_earning_base_clamps_earned_revenue_to_zero(self) -> None:
        # `max(0, gatedBase * deliveryPct)` in the reference. A change order larger than the
        # whole expected revenue (a downward CO) drives the gated base negative; earned revenue
        # floors at zero and the already-invoiced amount becomes deferred, never negative
        # "un-earned" income. Also pins the sign: the result must be 0.00, not -0.00.
        entry = _run(
            expected_revenue=40_000,
            expected_revenue_from_co=100_000,
            co_recognition_gated=True,
            delivery_pct=0.5,
            actual_invoiced=10_000,
        )
        assert entry["earned_revenue"] == Decimal("0.00")
        assert str(entry["earned_revenue"]) == "0.00"
        assert entry["target_accrued"] == Decimal("0.00")
        assert entry["target_deferred"] == Decimal("10000.00")

    def test_a_sub_ore_negative_product_clamps_to_an_unsigned_zero(self) -> None:
        # The narrow case a `max()`-based clamp gets wrong. -0.01 x 0.001 = -0.00001, which
        # quantises to Decimal("-0.00") — numerically EQUAL to zero, so `max()` keeps it and the
        # engine emits the string "-0.00" into a voucher. Value equality cannot catch this;
        # only the rendered sign can.
        entry = _run(
            expected_revenue=0,
            expected_revenue_from_co=Decimal("0.01"),
            co_recognition_gated=True,
            delivery_pct=0.001,
        )
        assert entry["earned_revenue"] == Decimal("0.00")
        assert str(entry["earned_revenue"]) == "0.00"
        assert entry["earned_revenue"].is_signed() is False

    def test_float_delivery_pct_keeps_its_decimal_value_not_its_binary_value(self) -> None:
        # Decimal(0.009) is 0.00899999999999999931998839741709... — the binary-float error. Only
        # Decimal(str(0.009)) is 0.009 exactly. 12345 x 0.009 = 111.105, which rounds to 111.11
        # from the exact value and to 111.10 from the binary one. One øre, on the cost matched
        # to revenue; multiplied over a project's lines it is a real reconciliation break.
        entry = _run(expected_cost=12_345, actual_cost=12_345, delivery_pct=0.009)
        assert entry["target_recognized_cogs"] == Decimal("111.11")
        assert entry["target_wip"] == Decimal("12233.89")


# ===========================================================================
# (b) WAVE — shape, exact summation, real-accrual proof, config-as-IP, fail-loud
# ===========================================================================


class TestShape:
    def test_returns_all_seven_buckets_in_canonical_order(self) -> None:
        result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, {})
        assert [entry["bucket"] for entry in result["buckets"]] == [
            "hardware",
            "materials",
            "freight",
            "pm",
            "tek",
            "programming",
            "travel",
        ]
        assert len(result["buckets"]) == 7

    def test_bucket_order_is_independent_of_caller_key_order(self) -> None:
        forwards = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {"buckets": {"hardware": {"actual_cost": 100}, "travel": {"actual_cost": 200}}},
        )
        backwards = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {"buckets": {"travel": {"actual_cost": 200}, "hardware": {"actual_cost": 100}}},
        )
        assert _split_of(forwards) == _split_of(backwards)

    def test_omitted_buckets_and_fields_default_to_zero(self) -> None:
        result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, {})
        for entry in result["buckets"]:
            for key in _AMOUNT_KEYS:
                assert entry[key] == Decimal("0"), f"{entry['bucket']}.{key}"

    def test_project_id_and_period_end_are_echoed(self) -> None:
        result = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {"project_id": "proj-1", "period_end": "2026-06-30"},
        )
        assert result["project_id"] == "proj-1"
        assert result["period_end"] == "2026-06-30"
        assert result["gaap"] == "NGAAP"
        assert result["country"] == "NO"

    def test_every_amount_is_a_decimal_never_a_float(self) -> None:
        # Floats cannot make the buckets sum exactly; if one leaks in, the whole exactness
        # argument is void. Guard the type, not just the value.
        result = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {"buckets": {"hardware": {"expected_cost": 10.5, "delivery_pct": 0.5}}},
        )
        for entry in result["buckets"]:
            for key in _AMOUNT_KEYS:
                assert isinstance(entry[key], Decimal), f"{entry['bucket']}.{key}"
        for name, total in result["totals"].items():
            assert isinstance(total, Decimal), name


class TestSevenBucketsSumToInputTotalExactly:
    """The binding requirement: no øre may be created or lost across the seven buckets.

    Two §4-1 identities, both checked with ``==`` and no tolerance:
      cost side    — Σ recognized_cogs + Σ wip == Σ actual_cost
      revenue side — Σ actual_invoiced + Σ accrued - Σ deferred == Σ earned_revenue
    """

    @staticmethod
    def _assert_totals_balance(result: dict, total_actual_cost: Decimal) -> None:
        totals = result["totals"]
        assert totals["actual_cost"] == total_actual_cost
        assert totals["recognized_cogs"] + totals["wip"] == total_actual_cost
        assert (
            totals["actual_invoiced"] + totals["accrued"] - totals["deferred"]
            == totals["earned_revenue"]
        )
        # And the totals really are the sum of the seven buckets, not a separately computed
        # number that happens to balance.
        for total_name, bucket_key in (
            ("recognized_cogs", "target_recognized_cogs"),
            ("wip", "target_wip"),
            ("accrued", "target_accrued"),
            ("deferred", "target_deferred"),
            ("unrecognized", "target_unrecognized"),
            ("earned_revenue", "earned_revenue"),
            ("actual_cost", "actual_cost"),
            ("actual_invoiced", "actual_invoiced"),
        ):
            assert totals[total_name] == sum(
                (entry[bucket_key] for entry in result["buckets"]), Decimal("0")
            ), total_name

    def test_thirds_across_all_seven_buckets_lose_no_ore(self) -> None:
        # delivery_pct = 1/3 is the classic drift case: 100.00 / 3 has no exact 2-dp value, so a
        # float engine that rounds each component independently leaves a residue. Seven buckets
        # multiply the chance of a stray øre sevenfold.
        one_third = Decimal(1) / Decimal(3)
        params = {
            "buckets": {
                bucket: {
                    "expected_revenue": Decimal("100.00"),
                    "expected_cost": Decimal("100.00"),
                    "actual_cost": Decimal("33.33"),
                    "actual_invoiced": Decimal("10.01"),
                    "delivery_pct": one_third,
                }
                for bucket in ALL_BUCKETS
            }
        }
        result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, params)
        self._assert_totals_balance(result, Decimal("33.33") * 7)

    def test_classic_float_drift_inputs_lose_no_ore(self) -> None:
        # 0.1 + 0.2 != 0.3 in binary float. Feed exactly those as money and demand exactness.
        params = {
            "buckets": {
                "hardware": {"actual_cost": 0.1, "expected_cost": 0.1, "delivery_pct": 1},
                "materials": {"actual_cost": 0.2, "expected_cost": 0.2, "delivery_pct": 1},
                "freight": {"actual_cost": 0.7, "expected_cost": 0.7, "delivery_pct": 1},
            }
        }
        result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, params)
        self._assert_totals_balance(result, Decimal("1.00"))
        assert result["totals"]["wip"] == Decimal("0.00")

    def test_mixed_signs_across_buckets_still_sum_exactly(self) -> None:
        # Some buckets ahead of cost (negative WIP), some behind (positive WIP), some
        # over-invoiced (deferred), some under (accrued). The totals must still close.
        params = {
            "buckets": {
                "hardware": {
                    "expected_cost": 80_000,
                    "actual_cost": 95_000,
                    "expected_revenue": 120_000,
                    "actual_invoiced": 0,
                    "delivery_pct": 0.5,
                },
                "materials": {
                    "expected_cost": 40_000,
                    "actual_cost": 5_000,
                    "expected_revenue": 60_000,
                    "actual_invoiced": 60_000,
                    "delivery_pct": 0.25,
                },
                "tek": {
                    "expected_cost": Decimal("33333.33"),
                    "actual_cost": Decimal("11111.11"),
                    "expected_revenue": Decimal("99999.99"),
                    "actual_invoiced": Decimal("12345.67"),
                    "delivery_pct": Decimal("0.37"),
                },
            }
        }
        result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, params)
        self._assert_totals_balance(
            result, Decimal("95000.00") + Decimal("5000.00") + Decimal("11111.11")
        )
        wips = [entry["target_wip"] for entry in result["buckets"]]
        assert any(wip > 0 for wip in wips) and any(wip < 0 for wip in wips)

    def test_per_bucket_identities_hold_for_every_bucket(self) -> None:
        params = {
            "buckets": {
                bucket: {
                    "expected_cost": 1234.56,
                    "actual_cost": 999.99,
                    "expected_revenue": 4321.12,
                    "actual_invoiced": 111.11,
                    "delivery_pct": 0.777,
                }
                for bucket in ALL_BUCKETS
            }
        }
        result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, params)
        for entry in result["buckets"]:
            assert entry["target_recognized_cogs"] + entry["target_wip"] == entry["actual_cost"], (
                entry["bucket"]
            )
            assert (
                entry["actual_invoiced"] + entry["target_accrued"] - entry["target_deferred"]
                == entry["earned_revenue"]
            ), entry["bucket"]


class TestRealAccrualNotFlatProRata:
    """The straddling-the-boundary split must be a genuine §4-1 accrual.

    The null hypothesis these tests must kill is a **flat pro-rata allocation**: something that
    takes the input total and splits it by a single non-negative weight (elapsed days, a fixed
    percentage), so that every component is a non-negative fraction of the total and every
    component scales together. Each test below is chosen so it PASSES under the accrual and
    FAILS under that null — a case both models satisfy proves nothing.
    """

    def test_wip_goes_negative_which_no_pro_rata_split_can_do(self) -> None:
        # Delivered 50% of a job estimated at 80k but only 30k of cost has landed. §4-1 nr. 3
        # matches 40k of cost to the recognised revenue, so 10k of cost is ACCRUED — WIP is
        # -10 000. A pro-rata split of a positive 30 000 total by non-negative weights can never
        # produce a negative component, so this assertion alone falsifies the null.
        entry = _run(expected_cost=80_000, actual_cost=30_000, delivery_pct=0.5)
        assert entry["target_wip"] == Decimal("-10000.00")
        assert entry["target_wip"] < 0
        # ...and the recognised half EXCEEDS the whole input total, which a fraction cannot.
        assert entry["target_recognized_cogs"] > entry["actual_cost"]

    def test_recognized_cogs_tracks_delivery_not_spend(self) -> None:
        # Same delivery, three different actual spends. Under pro-rata, recognised COGS is a
        # fraction OF the spend and must move with it. Under §4-1 nr. 3 it is anchored to
        # expected_cost × delivery and does not move at all; the entire difference lands in WIP,
        # one krone for one krone.
        results = [
            _run(expected_cost=80_000, actual_cost=spend, delivery_pct=0.5)
            for spend in (30_000, 40_000, 50_000)
        ]
        assert {entry["target_recognized_cogs"] for entry in results} == {Decimal("40000.00")}
        assert [entry["target_wip"] for entry in results] == [
            Decimal("-10000.00"),
            Decimal("0.00"),
            Decimal("10000.00"),
        ]

    def test_split_moves_with_delivery_while_the_input_total_is_unchanged(self) -> None:
        # Identical actual_cost, identical everything but progress. A time-proration model with
        # the same elapsed period would return the same split for all three.
        splits = [
            (
                entry["target_recognized_cogs"],
                entry["target_wip"],
            )
            for entry in (
                _run(expected_cost=100_000, actual_cost=50_000, delivery_pct=pct)
                for pct in (0.25, 0.5, 0.75)
            )
        ]
        assert splits == [
            (Decimal("25000.00"), Decimal("25000.00")),
            (Decimal("50000.00"), Decimal("0.00")),
            (Decimal("75000.00"), Decimal("-25000.00")),
        ]

    def test_accrued_and_deferred_are_mutually_exclusive(self) -> None:
        # An accrual produces exactly ONE side of the revenue gap. A pro-rata allocation that
        # split revenue across both drawers would put a non-zero amount in each.
        under = _run(expected_revenue=100_000, delivery_pct=0.6, actual_invoiced=10_000)
        over = _run(expected_revenue=100_000, delivery_pct=0.6, actual_invoiced=90_000)
        assert (under["target_accrued"], under["target_deferred"]) == (
            Decimal("50000.00"),
            Decimal("0.00"),
        )
        assert (over["target_accrued"], over["target_deferred"]) == (
            Decimal("0.00"),
            Decimal("30000.00"),
        )

    def test_invoicing_alone_flips_the_boundary_treatment(self) -> None:
        # Same delivery, same cost, same expected revenue — only the invoicing differs, and the
        # amount moves from the asset side (1531-class) to the liability side (2901-class).
        # Billing is invisible to a time/progress pro-rata; it is decisive under §4-1 nr. 2.
        common = {"expected_revenue": 100_000, "delivery_pct": 0.5, "expected_cost": 50_000}
        unbilled = _run(**common, actual_invoiced=0)
        overbilled = _run(**common, actual_invoiced=80_000)
        assert unbilled["target_accrued"] == Decimal("50000.00")
        assert overbilled["target_deferred"] == Decimal("30000.00")
        # The COST side is untouched by billing — the two accruals are independent.
        assert unbilled["target_recognized_cogs"] == overbilled["target_recognized_cogs"]
        assert unbilled["target_wip"] == overbilled["target_wip"]

    def test_period_end_does_not_influence_any_amount(self) -> None:
        # Structural proof that no time-proration is happening: the engine takes period_end only
        # to echo it. Two different closes with the same delivery periodise identically.
        params = {
            "period_end": "2026-06-30",
            "buckets": {
                "hardware": {"expected_cost": 100_000, "actual_cost": 10_000, "delivery_pct": 0.5}
            },
        }
        june = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, params)
        december = do_compute_bucket_targets(
            _FIXTURE_CHART, _FIXTURE_MAPPING, {**params, "period_end": "2026-12-31"}
        )
        assert _split_of(june) == _split_of(december)

    def test_gated_change_order_is_held_out_of_earned_revenue(self) -> None:
        # A.7: an unapproved change order is delivered work that is NOT yet earned revenue. A
        # flat allocation of contract value would recognise it; the accrual parks it in
        # `unrecognized`, out of the P&L, and the accrued asset stays at the approved amount.
        gated = _run(
            expected_revenue=150_000,
            expected_revenue_from_co=50_000,
            delivery_pct=0.5,
            co_recognition_gated=True,
        )
        approved = _run(
            expected_revenue=150_000,
            expected_revenue_from_co=50_000,
            delivery_pct=0.5,
            co_recognition_gated=False,
        )
        assert gated["earned_revenue"] == Decimal("50000.00")
        assert gated["target_accrued"] == Decimal("50000.00")
        assert gated["target_unrecognized"] == Decimal("25000.00")
        assert approved["earned_revenue"] == Decimal("75000.00")
        assert approved["target_accrued"] == Decimal("75000.00")
        assert approved["target_unrecognized"] == Decimal("0.00")


class TestConfigIsAccountsOnly:
    """Round-2 #5: swapping config must move the ACCOUNTS and not one øre of the SPLIT."""

    _PARAMS: dict = {
        "buckets": {
            bucket: {
                "expected_revenue": 111_111,
                "expected_cost": 77_777,
                "actual_cost": 55_555,
                "actual_invoiced": 22_222,
                "delivery_pct": 0.63,
                "expected_revenue_from_co": 11_111,
                "co_recognition_gated": True,
            }
            for bucket in ALL_BUCKETS
        }
    }

    def test_swapping_the_chart_changes_accounts_but_not_the_split(self) -> None:
        base = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, self._PARAMS)
        swapped = do_compute_bucket_targets(_swapped_chart(), _FIXTURE_MAPPING, self._PARAMS)

        assert _split_of(base) == _split_of(swapped)
        assert base["totals"] == swapped["totals"]

        base_accounts, swapped_accounts = _accounts_of(base), _accounts_of(swapped)
        assert base_accounts != swapped_accounts
        # EVERY account moved, not just one — a partial swap would mean some target is hard-coded.
        for before, after in zip(base_accounts, swapped_accounts):
            assert set(before) == set(after)
            for role in before:
                assert before[role] != after[role], role

    def test_real_example_config_produces_the_identical_split(self) -> None:
        # The strongest form of the claim: the tenant's REAL chart/mapping and the fixture's fake
        # 9xxx accounts periodise to the same numbers. If any split logic had leaked into the
        # JSON, these two would diverge.
        real = do_compute_bucket_targets(
            load_finago_chart_of_accounts(), load_finago_account_mapping(), self._PARAMS
        )
        fixture = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, self._PARAMS)
        assert _split_of(real) == _split_of(fixture)
        assert real["totals"] == fixture["totals"]
        assert _accounts_of(real) != _accounts_of(fixture)

    def test_mva_codes_come_from_the_mapping_not_the_code(self) -> None:
        base = _run(expected_revenue=1000, delivery_pct=1)
        assert base["accounts"]["revenue"]["mva_code"] == 3
        assert base["accounts"]["accrued"]["mva_code"] == 0

        remapped = copy.deepcopy(_FIXTURE_MAPPING)
        remapped["role_mva_code"]["revenue"] = 31  # e.g. a low-rate regime
        entry = _run(expected_revenue=1000, delivery_pct=1, mapping=remapped)
        assert entry["accounts"]["revenue"]["mva_code"] == 31
        assert _amounts(entry) == _amounts(base)

    def test_per_account_mva_override_wins_over_the_role_default(self) -> None:
        overridden = copy.deepcopy(_FIXTURE_MAPPING)
        revenue_account = _FIXTURE_BUCKET_ACCOUNTS["hardware"]["revenue"]
        overridden["account_mva_overrides"] = {revenue_account: 0}
        entry = _run(expected_revenue=1000, delivery_pct=1, mapping=overridden)
        assert entry["accounts"]["revenue"]["mva_code"] == 0
        # A different bucket's revenue account keeps the role default — the override is scoped.
        result = do_compute_bucket_targets(_FIXTURE_CHART, overridden, {})
        travel = next(e for e in result["buckets"] if e["bucket"] == "travel")
        assert travel["accounts"]["revenue"]["mva_code"] == 3

    def test_balance_side_comes_from_the_mapping(self) -> None:
        """The resolved side is the account's NATURAL BALANCE side, never a posting direction.

        Pre-fix this field was named ``role_posting_side`` in the JSON and surfaced as
        ``posting_side`` on every resolved target, and this test pinned that name with no
        statement of meaning. The name was wrong and dangerous: the reference
        (``cost-engine.ts``) debits revenue account 3000 in the deferred leg and credits the
        same 3000 in the accrued leg, and credits WIP 1771 by ``-cogsDelta`` — while this map
        calls both "credit"/"debit" respectively. A B119/B120 author deriving posting direction
        from the field mis-signs a leg (an ordinary over-invoiced bucket is a 60 000 NOK swing
        in reported revenue), and a voucher-balance guard cannot catch it because both sign
        orderings sum to zero. Renamed to ``role_balance_side`` / ``balance_side`` because the
        natural-balance metadata is genuinely useful; the footgun was only ever the name.
        """
        entry = _run()
        sides = {role: target["balance_side"] for role, target in entry["accounts"].items()}
        assert sides == {
            "cogs": "debit",
            "revenue": "credit",
            "accrued": "debit",
            "deferred": "credit",
            "wip": "debit",
        }
        # The old, misleading name is gone from the resolved target entirely — a consumer that
        # still reads `posting_side` must break loudly rather than silently pick up a side.
        assert all("posting_side" not in target for target in entry["accounts"].values())

    def test_balance_side_does_not_track_the_sign_of_the_amount(self) -> None:
        """The documented meaning, asserted: ``balance_side`` is constant while the leg's real
        direction flips with the sign of its amount.

        Two runs that differ only in invoicing: one accrues (asset side), one defers (liability
        side), and WIP goes from positive to negative — i.e. the actual debit/credit direction
        of the WIP leg reverses. ``balance_side`` is identical in both. Anything deriving a
        posting direction from it would post one of these two backwards.
        """
        under = _run(
            expected_revenue=100_000,
            expected_cost=80_000,
            actual_cost=50_000,
            actual_invoiced=0,
            delivery_pct=0.5,
        )
        over = _run(
            expected_revenue=100_000,
            expected_cost=80_000,
            actual_cost=30_000,
            actual_invoiced=100_000,
            delivery_pct=0.5,
        )
        assert under["target_wip"] > 0 and over["target_wip"] < 0
        assert under["target_accrued"] > 0 and over["target_deferred"] > 0
        for role in ("cogs", "revenue", "accrued", "deferred", "wip"):
            assert (
                under["accounts"][role]["balance_side"] == over["accounts"][role]["balance_side"]
            ), role

    def test_wip_account_is_shared_across_all_seven_buckets(self) -> None:
        result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, {})
        wip_accounts = {entry["accounts"]["wip"]["account"] for entry in result["buckets"]}
        assert wip_accounts == {_FIXTURE_SHARED_ACCOUNTS["wip"]}


class TestFailLoudBoundary:
    """Untyped dicts are hostile. Every one of these would otherwise post a wrong number."""

    def test_unknown_bucket_name_raises_instead_of_being_dropped(self) -> None:
        with pytest.raises(ValueError, match="unknown bucket"):
            do_compute_bucket_targets(
                _FIXTURE_CHART, _FIXTURE_MAPPING, {"buckets": {"hardwares": {"actual_cost": 1}}}
            )

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("bucket", id="singular"),
            pytest.param("buckets ", id="trailing-space"),
            pytest.param(" buckets", id="leading-space"),
            pytest.param("Buckets", id="capitalised"),
            pytest.param("BUCKETS", id="shouting"),
        ],
    )
    def test_a_typod_top_level_buckets_key_raises_instead_of_losing_all_seven(
        self, key: str
    ) -> None:
        """Pre-fix: ``do_compute_bucket_targets`` read ``params.get("buckets")`` raw, so every
        near-miss below silently produced a COMPLETE, apparently-successful periodisering with
        all seven buckets at zero — ``actual_cost 0.00``, no exception, and both §4-1 identities
        satisfied because 0 == 0. The guard one level down already refused a typo'd BUCKET name
        on the grounds that losing one bucket's whole cost is unacceptable; this key loses all
        seven at once. Note ``" buckets"``/``"buckets "``: key normalisation now strips them, so
        those two are ACCEPTED as ``buckets`` rather than raising — either way the cost is no
        longer lost.
        """
        params = {key: {"hardware": {"actual_cost": 1000, "expected_cost": 1000}}}
        if key.strip() == "buckets":
            result = do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, params)
            assert result["totals"]["actual_cost"] == Decimal("1000.00")
            return
        with pytest.raises(ValueError, match="params has unknown key"):
            do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, params)

    def test_a_typod_project_id_key_raises_instead_of_silently_dropping_the_project(self) -> None:
        """Pre-fix ``{"projectId": "p-1"}`` returned ``project_id: None`` with no error — the
        periodisering was no longer attributable to a project."""
        with pytest.raises(ValueError, match=r"unknown key\(s\) \['projectId'\]"):
            do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, {"projectId": "p-1"})

    def test_unknown_top_level_param_names_the_offender(self) -> None:
        with pytest.raises(ValueError, match=r"unknown key\(s\) \['period_ends'\]"):
            do_compute_bucket_targets(
                _FIXTURE_CHART, _FIXTURE_MAPPING, {"period_ends": "2026-06-30"}
            )

    def test_every_documented_top_level_param_key_is_accepted(self) -> None:
        """The allow-list must not be tighter than the documented contract."""
        result = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {
                "_comment": "a run note",
                "project_id": "proj-1",
                "period_end": "2026-06-30",
                "buckets": {"hardware": {"actual_cost": 5}},
            },
        )
        assert result["project_id"] == "proj-1"
        assert result["period_end"] == "2026-06-30"
        assert result["totals"]["actual_cost"] == Decimal("5.00")

    def test_top_level_params_keys_are_normalised_before_being_echoed(self) -> None:
        """Normalise once, read only the copy (Batch 116's worst defect was doing the
        opposite): a padded ``" project_id "`` must reach the echoed output, not be dropped."""
        result = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {" project_id ": "proj-1", "period_end ": "2026-06-30"},
        )
        assert result["project_id"] == "proj-1"
        assert result["period_end"] == "2026-06-30"

    def test_colliding_top_level_params_keys_raise(self) -> None:
        with pytest.raises(ValueError, match="normalise to 'buckets'"):
            do_compute_bucket_targets(
                _FIXTURE_CHART,
                _FIXTURE_MAPPING,
                {
                    "buckets": {"hardware": {"actual_cost": 1}},
                    " buckets": {"hardware": {"actual_cost": 2}},
                },
            )

    def test_non_object_params_raises(self) -> None:
        with pytest.raises(ValueError, match="params must be an object"):
            do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, [])  # type: ignore[arg-type]

    def test_unknown_field_name_raises_instead_of_being_ignored(self) -> None:
        with pytest.raises(ValueError, match="unknown key"):
            _run(actual_costs=1000)

    def test_colliding_bucket_keys_raise(self) -> None:
        with pytest.raises(ValueError, match="normalise to 'hardware'"):
            do_compute_bucket_targets(
                _FIXTURE_CHART,
                _FIXTURE_MAPPING,
                {"buckets": {"hardware": {"actual_cost": 1}, " hardware ": {"actual_cost": 2}}},
            )

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("30000", id="string-amount"),
            pytest.param(float("nan"), id="nan-truthy-in-python-falsy-in-js"),
            pytest.param(float("inf"), id="inf"),
            pytest.param(Decimal("NaN"), id="decimal-nan"),
            pytest.param(True, id="bool-is-an-int-in-python"),
            pytest.param([0], id="truthy-list"),
            pytest.param(object(), id="object"),
        ],
    )
    def test_unusable_amount_raises_rather_than_degrading_to_zero(self, value: object) -> None:
        with pytest.raises(ValueError, match="actual_cost"):
            _run(actual_cost=value)

    def test_none_amount_is_treated_as_absent(self) -> None:
        assert _run(actual_cost=None)["actual_cost"] == Decimal("0.00")

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="inf"),
            pytest.param(float("-inf"), id="-inf"),
            pytest.param(Decimal("NaN"), id="decimal-nan"),
            pytest.param(Decimal("Infinity"), id="decimal-inf"),
            pytest.param("0.5", id="string"),
            pytest.param(True, id="bool"),
        ],
    )
    def test_unusable_delivery_pct_raises_naming_the_field(self, value: object) -> None:
        # delivery_pct is the one field that is NOT quantised, so it has no downstream
        # quantise() to accidentally catch a NaN for it. A NaN that reaches the multiply either
        # poisons every amount silently or raises a bare InvalidOperation from deep inside the
        # engine; the guard must fire here, at the boundary, naming the field.
        with pytest.raises(ValueError, match="delivery_pct"):
            _run(delivery_pct=value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(-0.5, id="float"),
            pytest.param(Decimal("-0.5"), id="decimal"),
            pytest.param(-1, id="int"),
            pytest.param(Decimal("-0.0000001"), id="sub-ore-negative"),
        ],
    )
    def test_negative_delivery_pct_raises(self, value: object) -> None:
        """Pre-fix ``delivery_pct=-0.5`` returned ``earned_revenue 0.00`` together with
        ``recognized_cogs -40 000.00`` — cost matched against zero revenue, the exact inverse of
        the §4-1 nr. 3 sammenstillingsprinsipp this module cites as its reason for existing. It
        slipped past BOTH identity guards: WIP is the exact residual so it absorbed the whole
        wrong-signed COGS and ``recognized_cogs + wip == actual_cost`` still held. Only a
        boundary check can catch it.
        """
        with pytest.raises(ValueError, match="delivery_pct must not be negative"):
            _run(expected_revenue=100_000, expected_cost=80_000, delivery_pct=value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(50, id="percent-as-int"),
            pytest.param(50.0, id="percent-as-float"),
            pytest.param(Decimal("100"), id="percent-100"),
            pytest.param(Decimal("10.000001"), id="just-over-the-ceiling"),
        ],
    )
    def test_delivery_pct_above_the_sanity_ceiling_raises(self, value: object) -> None:
        """Pre-fix ``delivery_pct=50`` (a caller passing "50 %") earned 5 000 000.00 on a
        100 000 NOK contract with no error and both identities satisfied. The module invites the
        confusion itself by reporting ``recognition_basis_pct = delivery_pct * 100``. The 1.0
        over-delivery cap stays deliberately absent (reference parity); the ceiling is 10.
        """
        with pytest.raises(ValueError, match=r"delivery_pct must be a RATIO in \[0, 10\]"):
            _run(expected_revenue=100_000, delivery_pct=value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="floor"),
            pytest.param(1.05, id="reference-over-delivery"),
            pytest.param(Decimal("10"), id="ceiling-inclusive"),
        ],
    )
    def test_delivery_pct_at_the_bounds_is_accepted(self, value: object) -> None:
        """Both bounds are inclusive, and 1.05 — the reference's "Engine-en capper IKKE" case —
        must still flow through untouched."""
        entry = _run(expected_revenue=100_000, delivery_pct=value)
        assert entry["earned_revenue"] == Decimal(100_000) * Decimal(str(value))

    def test_unusable_amount_error_names_the_field_not_just_the_rounding_step(self) -> None:
        # Pins WHERE the NaN is caught, not merely that something raised: quantize() also
        # rejects NaN, so a removed boundary guard would still raise — with a misleading
        # "too large to express in øre" message about a value that is not large at all.
        with pytest.raises(ValueError, match="actual_cost must be finite"):
            _run(actual_cost=float("nan"))

    def test_sub_ore_input_is_quantised_at_the_boundary(self) -> None:
        # Money is quantised ONCE, on the way in. If a 3-dp input flowed through unquantised,
        # WIP (the exact residual) would inherit the third decimal and put a fraction of an øre
        # into the ledger — and the "input total" the buckets sum to would itself be un-postable.
        entry = _run(actual_cost=Decimal("100.005"), expected_cost=0, delivery_pct=0)
        assert entry["actual_cost"] == Decimal("100.01")
        assert entry["target_wip"] == Decimal("100.01")
        for key in ("actual_cost", "target_wip", "target_recognized_cogs"):
            assert entry[key].as_tuple().exponent == -2, key

    def test_every_returned_amount_is_expressed_in_whole_ore(self) -> None:
        result = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {
                "buckets": {
                    bucket: {
                        "expected_revenue": Decimal("1000.004"),
                        "expected_cost": Decimal("500.006"),
                        "actual_cost": Decimal("333.335"),
                        "actual_invoiced": Decimal("99.999"),
                        "delivery_pct": Decimal("0.3333"),
                    }
                    for bucket in ALL_BUCKETS
                }
            },
        )
        money_keys = [key for key in _AMOUNT_KEYS if key != "recognition_basis_pct"]
        for entry in result["buckets"]:
            for key in money_keys:
                assert entry[key].as_tuple().exponent == -2, f"{entry['bucket']}.{key}"
        for name, total in result["totals"].items():
            assert total.as_tuple().exponent == -2, name

    def test_bucket_field_disagreeing_with_its_key_raises(self) -> None:
        with pytest.raises(ValueError, match="the key and the 'bucket' field must agree"):
            do_compute_bucket_targets(
                _FIXTURE_CHART,
                _FIXTURE_MAPPING,
                {"buckets": {"hardware": {"bucket": "travel", "actual_cost": 1}}},
            )

    def test_bucket_field_agreeing_with_its_key_is_accepted(self) -> None:
        result = do_compute_bucket_targets(
            _FIXTURE_CHART,
            _FIXTURE_MAPPING,
            {"buckets": {"hardware": {"bucket": "hardware", "actual_cost": 1}}},
        )
        assert result["buckets"][0]["actual_cost"] == Decimal("1.00")

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("false", id="truthy-string-false"),
            pytest.param(1, id="int-one"),
            pytest.param(0, id="int-zero"),
            pytest.param(float("nan"), id="nan"),
            pytest.param([0], id="truthy-list"),
        ],
    )
    def test_non_bool_recognition_gate_raises(self, value: object) -> None:
        # Direction matters: silently reading a bad gate as False would RECOGNISE change-order
        # revenue the customer never approved. That is the permissive direction — refuse it.
        with pytest.raises(ValueError, match="co_recognition_gated"):
            _run(co_recognition_gated=value)

    def test_absent_recognition_gate_defaults_to_not_gated(self) -> None:
        assert _run(expected_revenue=100, expected_revenue_from_co=40, delivery_pct=1)[
            "earned_revenue"
        ] == Decimal("100.00")

    def test_decimal_from_a_postgres_numeric_column_is_accepted(self) -> None:
        # The repo has no set_type_codec, so a `numeric` column arrives as Decimal. Mixing it
        # with a float in the same call must not raise TypeError.
        entry = _run(expected_cost=Decimal("80000.00"), actual_cost=30000.0, delivery_pct=0.5)
        assert entry["target_wip"] == Decimal("-10000.00")

    def test_chart_missing_an_account_for_a_role_raises(self) -> None:
        broken = copy.deepcopy(_FIXTURE_CHART)
        del broken["bucket_accounts"]["freight"]["deferred"]
        with pytest.raises(ValueError, match="no account for bucket='freight' role='deferred'"):
            do_compute_bucket_targets(broken, _FIXTURE_MAPPING, {})

    def test_chart_referencing_an_account_outside_the_plan_raises(self) -> None:
        broken = copy.deepcopy(_FIXTURE_CHART)
        broken["bucket_accounts"]["pm"]["cogs"] = "0000"
        with pytest.raises(ValueError, match="'accounts' has no entry for account '0000'"):
            do_compute_bucket_targets(broken, _FIXTURE_MAPPING, {})

    def test_chart_missing_a_whole_bucket_raises(self) -> None:
        broken = copy.deepcopy(_FIXTURE_CHART)
        del broken["bucket_accounts"]["travel"]
        with pytest.raises(ValueError, match="missing an object for bucket 'travel'"):
            do_compute_bucket_targets(broken, _FIXTURE_MAPPING, {})

    def test_chart_missing_the_shared_wip_account_raises(self) -> None:
        broken = copy.deepcopy(_FIXTURE_CHART)
        broken["shared_accounts"] = {}
        with pytest.raises(ValueError, match="role='wip'"):
            do_compute_bucket_targets(broken, _FIXTURE_MAPPING, {})

    @pytest.mark.parametrize(
        "mva", [pytest.param(True, id="bool"), pytest.param("3", id="string"), None]
    )
    def test_non_integer_mva_code_raises(self, mva: object) -> None:
        broken = copy.deepcopy(_FIXTURE_MAPPING)
        broken["role_mva_code"]["revenue"] = mva
        with pytest.raises(ValueError, match="no integer MVA code"):
            do_compute_bucket_targets(_FIXTURE_CHART, broken, {})

    def test_bad_balance_side_raises(self) -> None:
        """Pre-fix this matched ``role_posting_side``; the field was renamed to
        ``role_balance_side`` (see ``test_balance_side_comes_from_the_mapping``)."""
        broken = copy.deepcopy(_FIXTURE_MAPPING)
        broken["role_balance_side"]["wip"] = "left"
        with pytest.raises(ValueError, match="role_balance_side"):
            do_compute_bucket_targets(_FIXTURE_CHART, broken, {})

    def test_mapping_still_using_the_old_posting_side_key_raises(self) -> None:
        """A mapping JSON that was not migrated must fail loud, not resolve to nothing.

        ``role_balance_side`` is required, so an un-migrated file carrying only the old
        ``role_posting_side`` key raises rather than silently resolving every target's side.
        """
        stale = copy.deepcopy(_FIXTURE_MAPPING)
        stale["role_posting_side"] = stale.pop("role_balance_side")
        with pytest.raises(ValueError, match="'role_balance_side' must be an object"):
            do_compute_bucket_targets(_FIXTURE_CHART, stale, {})

    def test_non_object_buckets_param_raises(self) -> None:
        with pytest.raises(ValueError, match=r"params\['buckets'\] must be an object"):
            do_compute_bucket_targets(_FIXTURE_CHART, _FIXTURE_MAPPING, {"buckets": []})

    def test_non_object_bucket_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="must be an object"):
            do_compute_bucket_targets(
                _FIXTURE_CHART, _FIXTURE_MAPPING, {"buckets": {"hardware": 5}}
            )


class TestOneExceptionType:
    """``ValueError`` is the only exception this module raises for a bad number.

    Pre-fix, ``delivery_pct=Decimal("1E+999999")`` (unbounded, and unquantised so nothing
    checked its magnitude) escaped as ``decimal.Overflow`` — an ``ArithmeticError``, neither a
    ``ValueError`` nor an ``InvalidOperation`` — straight out of ``gated_base * delivery_pct``,
    so a caller that correctly wrapped the periodisering in ``except ValueError`` still crashed.
    """

    def test_an_astronomically_large_delivery_pct_raises_valueerror_not_overflow(self) -> None:
        with pytest.raises(ValueError):
            _run(expected_revenue=100_000, delivery_pct=Decimal("1E+999999"))

    def test_an_astronomically_large_money_amount_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="too large to express in øre"):
            _run(expected_revenue=Decimal("1E+999999"), delivery_pct=1)

    def test_product_translates_a_decimal_overflow(self) -> None:
        """The ratio ceiling (FIX 3) closes the public route to an overflowing product, so this
        exercises the translation layer directly — it is the defence that keeps a raw
        ``decimal`` exception from ever reaching a caller again if a future edit reopens one.
        """
        huge = Decimal("1E+999999")
        with pytest.raises(decimal.Overflow):  # what the bare multiply does
            huge * huge
        with pytest.raises(ValueError, match="earned_revenue is not expressible as a number"):
            _product(huge, huge, "earned_revenue")


# ===========================================================================
# (c) CONFIG — the two real JSON files parse and carry the documented keys
# ===========================================================================


class TestTenantConfigFiles:
    def test_chart_of_accounts_parses_and_carries_documented_keys(self) -> None:
        chart = load_finago_chart_of_accounts()
        assert chart["country"] == "NO"
        assert chart["gaap"] == "NGAAP"
        assert chart["buckets"] == list(ALL_BUCKETS)
        assert set(chart["bucket_accounts"]) == set(ALL_BUCKETS)
        for bucket, roles in chart["bucket_accounts"].items():
            assert set(roles) == {"cogs", "revenue", "accrued", "deferred"}, bucket
        assert chart["shared_accounts"] == {"wip": "1771"}

    def test_chart_carries_the_documented_norwegian_accounts(self) -> None:
        # docs/vertical_engines/08-economy-engine.md: "the Norwegian chart of accounts
        # (1531/2901/1771/4300…)", matching cost-engine.ts's account constants.
        chart = load_finago_chart_of_accounts()
        assert chart["bucket_accounts"]["hardware"] == {
            "cogs": "4300",
            "revenue": "3000",
            "accrued": "1531",
            "deferred": "2901",
        }
        assert chart["bucket_accounts"]["travel"]["cogs"] == "4160"
        assert chart["bucket_accounts"]["freight"]["cogs"] == "4060"
        for account in ("1531", "2901", "1771", "4300"):
            assert account in chart["accounts"], account
            assert chart["accounts"][account]["name"]
            assert chart["accounts"][account]["type"] in {
                "asset",
                "liability",
                "revenue",
                "expense",
            }

    def test_every_referenced_account_exists_in_the_plan(self) -> None:
        chart = load_finago_chart_of_accounts()
        referenced = {
            account for roles in chart["bucket_accounts"].values() for account in roles.values()
        } | set(chart["shared_accounts"].values())
        assert referenced <= set(chart["accounts"])

    def test_account_mapping_parses_and_carries_documented_keys(self) -> None:
        mapping = load_finago_account_mapping()
        assert set(mapping["roles"]) == {"cogs", "revenue", "accrued", "deferred", "wip"}
        assert set(mapping["role_mva_code"]) == set(mapping["roles"])
        # Renamed from `role_posting_side` — see test_balance_side_comes_from_the_mapping.
        assert set(mapping["role_balance_side"]) == set(mapping["roles"])
        assert "role_posting_side" not in mapping
        assert mapping["account_mva_overrides"] == {}
        # Revenue postings carry MVA code 3 (utgående, høy sats) exactly as cost-engine.ts
        # emits; every balance-sheet periodisering leg carries 0.
        assert mapping["role_mva_code"] == {
            "cogs": 0,
            "revenue": 3,
            "accrued": 0,
            "deferred": 0,
            "wip": 0,
        }
        assert set(mapping["mva_codes"]) == {"0", "3"}
        assert mapping["mva_codes"]["3"]["rate_pct"] == 25

    def test_config_files_contain_no_split_logic(self) -> None:
        # Round-2 #5 guard, enforced mechanically: neither JSON may carry a recognition rule.
        # If a future edit adds one of these keys, the design has been inverted — stop.
        forbidden = {
            "delivery_pct",
            "split",
            "splits",
            "allocation",
            "recognition_rule",
            "recognition_rules",
            "accrual_rule",
            "accrual_rules",
            "percentages",
            "curve",
            "proration",
        }
        for config in (load_finago_chart_of_accounts(), load_finago_account_mapping()):
            assert forbidden.isdisjoint(_all_keys(config))

    def test_real_config_reproduces_the_reference_case(self) -> None:
        # End-to-end with production config: Andreas's negative-WIP case, landing on the tenant's
        # real Norwegian accounts.
        entry = _run(
            chart=load_finago_chart_of_accounts(),
            mapping=load_finago_account_mapping(),
            expected_cost=80_000,
            actual_cost=30_000,
            expected_revenue=100_000,
            actual_invoiced=0,
            delivery_pct=0.5,
        )
        assert entry["target_recognized_cogs"] == Decimal("40000.00")
        assert entry["target_wip"] == Decimal("-10000.00")
        assert entry["target_accrued"] == Decimal("50000.00")
        assert entry["accounts"]["accrued"]["account"] == "1531"
        assert entry["accounts"]["deferred"]["account"] == "2901"
        assert entry["accounts"]["wip"]["account"] == "1771"
        assert entry["accounts"]["cogs"]["account"] == "4300"


def _all_keys(node: object) -> set[str]:
    """Every key appearing anywhere in a nested JSON structure."""
    if isinstance(node, dict):
        keys = set(node)
        for value in node.values():
            keys |= _all_keys(value)
        return keys
    if isinstance(node, list):
        return set().union(*(_all_keys(item) for item in node)) if node else set()
    return set()
