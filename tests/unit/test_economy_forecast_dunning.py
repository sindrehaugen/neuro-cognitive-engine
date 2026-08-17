"""
tests/unit/test_economy_forecast_dunning.py
=============================================
Acceptance tests for Batch 127 — Module 8.Wave 12 (forecast-dunning).

Two pure Advisor cores, tested in two sections:
  (a) FORECAST — ``do_forecast_cashflow`` (Monte Carlo cashflow). The acceptance criterion is
      determinism for a fixed seed. The single test that would fail if the RNG were sourced
      from the module-level ``random`` functions instead of an explicit ``random.Random(seed)``
      instance is ``test_forecast_never_touches_global_random_state`` below — it asserts
      ``random.getstate()`` is bit-for-bit unchanged by a call, which can only hold if the
      call never reads or reseeds the interpreter-wide Mersenne Twister.
  (b) DUNNING — ``do_compute_dunning`` (Bisnode risk-score -> aggression tier -> HW-signing).
      The acceptance criterion is that every tier boundary is pinned at its EXACT value (not
      just a value comfortably inside a band), and that a missing/unknown/out-of-range signal
      raises rather than silently choosing a tier.

All plain unit tests: no DB, no HTTP, no ``@pytest.mark.integration``.
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

import pytest

from nce.vertical_modules.economy.dunning import do_compute_dunning
from nce.vertical_modules.economy.forecast import do_forecast_cashflow

# ===========================================================================
# (a) FORECAST — do_forecast_cashflow
# ===========================================================================


def _params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "periods": [
            {"period": "2026-09", "expected_net": 100000, "uncertainty_pct": 0.1},
            {"period": "2026-10", "expected_net": -50000, "uncertainty_pct": 0.2},
        ],
        "iterations": 200,
        "opening_balance": 10000,
    }
    base.update(overrides)
    return base


# --- determinism -----------------------------------------------------------


def test_forecast_is_deterministic_for_fixed_seed() -> None:
    """The wave's binding acceptance criterion: same seed + same params -> identical result,
    element for element (including every nested Decimal)."""
    params = _params()
    first = do_forecast_cashflow(42, params)
    second = do_forecast_cashflow(42, params)
    assert first == second


def test_forecast_different_seeds_diverge() -> None:
    """Sanity complement: two different seeds over multiple periods/iterations produce a
    different result (astronomically unlikely to collide by chance)."""
    params = _params()
    first = do_forecast_cashflow(1, params)
    second = do_forecast_cashflow(2, params)
    assert first != second


def test_forecast_never_touches_global_random_state() -> None:
    """THE determinism-mechanism test named in the wave's Return: this is the test that fails
    if ``do_forecast_cashflow`` sourced its randomness from the module-level
    ``random.seed()``/``random.gauss()`` free functions instead of a private
    ``random.Random(seed)`` instance.

    The module-level functions read AND overwrite the interpreter-wide Mersenne Twister state
    as an observable side effect (``random.getstate()`` changes even if you immediately
    reseed to a fixed value — the state after a fixed reseed is not the state that was there
    before). A private ``random.Random(seed)`` instance can never touch that global state in
    either direction, so ``random.getstate()`` must be bit-for-bit IDENTICAL before and after
    the call.
    """
    before = random.getstate()
    do_forecast_cashflow(42, _params())
    after = random.getstate()
    assert before == after


def test_forecast_output_unaffected_by_prior_global_random_usage() -> None:
    """Complementary proof: perturbing the global ``random`` module differently before each
    call must not change the result for the same explicit seed."""
    params = _params()

    random.seed(1)
    first = do_forecast_cashflow(7, params)

    random.seed(999)
    for _ in range(50):
        random.random()
    second = do_forecast_cashflow(7, params)

    assert first == second


# --- exactness / Decimal boundary ------------------------------------------


def test_forecast_zero_uncertainty_is_a_single_point_every_iteration() -> None:
    """money-module briefing #2/#6: with uncertainty_pct=0 there is no scale to perturb, so
    every iteration must agree exactly, and the running balance must be the EXACT Decimal sum
    of already-quantised amounts (no drift)."""
    params = {
        "periods": [
            {"period": "P1", "expected_net": 1000, "uncertainty_pct": 0},
            {"period": "P2", "expected_net": -400, "uncertainty_pct": 0},
        ],
        "iterations": 50,
        "opening_balance": 500,
    }
    result = do_forecast_cashflow(1, params)
    p1, p2 = result["periods"]

    assert result["opening_balance"] == Decimal("500.00")
    assert p1["net_p10"] == p1["net_p50"] == p1["net_p90"] == Decimal("1000.00")
    assert p2["net_p10"] == p2["net_p50"] == p2["net_p90"] == Decimal("-400.00")
    assert p1["balance_p50"] == Decimal("1500.00")
    assert p2["balance_p50"] == Decimal("1100.00")

    for value in (
        p1["net_p10"],
        p1["balance_p50"],
        p2["net_p90"],
        p2["balance_p10"],
        result["opening_balance"],
    ):
        assert isinstance(value, Decimal)


def test_forecast_percentiles_are_monotonic_and_are_decimal() -> None:
    result = do_forecast_cashflow(3, _params(iterations=500))
    for period in result["periods"]:
        assert period["net_p10"] <= period["net_p50"] <= period["net_p90"]
        assert period["balance_p10"] <= period["balance_p50"] <= period["balance_p90"]
        for key in ("net_p10", "net_p50", "net_p90", "balance_p10", "balance_p50", "balance_p90"):
            assert isinstance(period[key], Decimal)


def test_forecast_echoes_period_labels_in_order() -> None:
    result = do_forecast_cashflow(1, _params())
    assert [p["period"] for p in result["periods"]] == ["2026-09", "2026-10"]


# --- golden-value regression tests: mutation-resistant exactness pins -------
#
# Every test above this point uses uncertainty_pct=0, which multiplies the noise term by exact
# zero -- it cannot discriminate `Decimal(str(z))` from `Decimal(z)`, nor "quantise per
# iteration" from "quantise once at aggregation", because with zero uncertainty there is no
# noise, and no rounding ambiguity, regardless of which of those two implementations is
# running. The two tests below use fixed seeds, fixed non-zero uncertainty_pct, and
# hand-derived amounts specifically chosen to land on/near an exact half-øre boundary, so the
# quantised OUTPUT genuinely differs between the correct implementation and each mutation.


def test_forecast_golden_value_pins_decimal_str_z_boundary() -> None:
    """Mutation kill #1: pins the float -> Decimal boundary (forecast.py's ``noise =
    Decimal(str(z)) * period.uncertainty_pct``, never ``Decimal(z)``).

    Derivation (seed=1, one period, one iteration)::

        z = random.Random(1).gauss(0.0, 1.0) == 1.2881847531554629
        z_str  = Decimal(str(z))  == Decimal('1.2881847531554629')          # 17 sig figs --
                                                                             # what forecast.py
                                                                             # actually uses.
        z_full = Decimal(z)       == Decimal('1.28818475315546285483...')  # the exact binary
                                                                             # expansion the
                                                                             # mutation would
                                                                             # import instead.
        delta = z_full - z_str == Decimal('-1.948606269288575276732444763E-17')   (tiny, < 0)

    ``expected_net=100000.00`` and ``uncertainty_pct`` (an exact ``Decimal`` literal, not a
    float -- so it carries every digit below unchanged) are hand-picked so that, under the
    CORRECT implementation::

        raw = expected_net * (1 + z_str * uncertainty_pct) == EXACTLY 200000.005

    an exact half-øre tie. ``ROUND_HALF_UP`` rounds a tie away from zero, so the correct
    implementation quantises this to ``200000.01``.

    Under the mutation (``Decimal(str(z))`` -> ``Decimal(z)``), the noise term picks up the
    extra ``delta`` term (``expected_net * uncertainty_pct * delta == -1.5126761607E-12``),
    shifting ``raw`` down to ``200000.0049999999984873238393`` -- strictly BELOW the tie, so it
    quantises to ``200000.00`` instead: a one-øre disagreement.

    Hand-verified (see the wave's Return for the actual RED/GREEN mutation run): applying the
    ``Decimal(str(z))`` -> ``Decimal(z)`` mutation at forecast.py's noise line turns this
    test's assertion from passing to failing (200000.00 != 200000.01).
    """
    uncertainty = Decimal("0.7762862023870859273762237265")
    params = {
        "periods": [
            {
                "period": "P1",
                "expected_net": Decimal("100000.00"),
                "uncertainty_pct": uncertainty,
            },
        ],
        "iterations": 1,
        "opening_balance": Decimal("0.00"),
    }
    result = do_forecast_cashflow(1, params)
    period = result["periods"][0]
    assert period["net_p10"] == period["net_p50"] == period["net_p90"] == Decimal("200000.01")
    assert period["balance_p50"] == Decimal("200000.01")


def test_forecast_golden_value_pins_per_iteration_quantisation() -> None:
    """Mutation kill #2: pins WHERE quantisation happens -- once per (period, iteration) in
    :func:`_run_iteration`, never delayed until after percentiles are aggregated.

    Derivation (seed=5, two periods, one iteration)::

        z0 = random.Random(5).gauss(0.0, 1.0)      == -1.1788417512306717  # period 1's draw
        z1 = <next gauss() on the same rng>        == -1.1481606807908016  # period 2's draw

    ``expected_net=1.00`` for both periods; each period's ``uncertainty_pct`` (an exact
    ``Decimal`` literal) is hand-picked so that each period's RAW (unquantised) simulated net
    is EXACTLY ``0.004`` -- comfortably below the half-øre tie, so each period rounds DOWN to
    ``0.00`` when quantised on its own::

        raw_net1 = 1.00 * (1 + z0 * 0.8448971195329729589058522593) == 0.004000...
        raw_net2 = 1.00 * (1 + z1 * 0.8674744020270750387636909984) == 0.004000...

    Per-iteration quantisation (correct, what forecast.py does)::

        balance_2 = 0.00 + quantise(0.004) + quantise(0.004) = 0.00 + 0.00 + 0.00 = 0.00

    Aggregate-then-quantise (the mutation: carry raw nets/balances through the whole
    simulation and quantise only once, after ``_percentile`` picks the representative raw
    value)::

        balance_2 = quantise(0.00 + 0.004 + 0.004) = quantise(0.008) = 0.01

    The two implementations disagree by exactly one øre on period 2's running balance -- a
    classic sum-of-rounded vs. round-of-sum divergence, not a knife-edge float tie, so it is
    robust to how exactly the mutation is expressed. This test pins the per-iteration
    (correct) answer, ``0.00``.
    """
    uncertainty_1 = Decimal("0.8448971195329729589058522593")
    uncertainty_2 = Decimal("0.8674744020270750387636909984")
    params = {
        "periods": [
            {"period": "P1", "expected_net": Decimal("1.00"), "uncertainty_pct": uncertainty_1},
            {"period": "P2", "expected_net": Decimal("1.00"), "uncertainty_pct": uncertainty_2},
        ],
        "iterations": 1,
        "opening_balance": Decimal("0.00"),
    }
    result = do_forecast_cashflow(5, params)
    p1, p2 = result["periods"]
    assert p1["net_p50"] == Decimal("0.00")
    assert p2["net_p50"] == Decimal("0.00")
    assert p2["balance_p50"] == Decimal("0.00")
    assert p1["balance_p50"] == Decimal("0.00")


# --- coercion boundary: seed ------------------------------------------------


@pytest.mark.parametrize("bad_seed", [None, True, False, 3.0, "3", [3]])
def test_forecast_rejects_bad_seed(bad_seed: Any) -> None:
    with pytest.raises(ValueError):
        do_forecast_cashflow(bad_seed, _params())


# --- coercion boundary: iterations ------------------------------------------


@pytest.mark.parametrize("bad_iterations", [0, -1, 100_001, True, 1.5, "10"])
def test_forecast_rejects_bad_iterations(bad_iterations: Any) -> None:
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, _params(iterations=bad_iterations))


def test_forecast_iterations_defaults_when_absent() -> None:
    params = _params()
    del params["iterations"]
    result = do_forecast_cashflow(1, params)
    assert result["iterations"] == 1000


# --- coercion boundary: opening_balance -------------------------------------


@pytest.mark.parametrize("bad_opening", [True, float("nan"), float("inf"), "1000", [0]])
def test_forecast_rejects_bad_opening_balance(bad_opening: Any) -> None:
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, _params(opening_balance=bad_opening))


def test_forecast_opening_balance_defaults_to_zero() -> None:
    params = _params()
    del params["opening_balance"]
    result = do_forecast_cashflow(1, params)
    assert result["opening_balance"] == Decimal("0.00")


# --- coercion boundary: periods / expected_net / uncertainty_pct -----------


def test_forecast_rejects_empty_periods() -> None:
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, _params(periods=[]))


def test_forecast_rejects_non_list_periods() -> None:
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, _params(periods={"a": 1}))


def test_forecast_rejects_too_many_periods() -> None:
    """FIX 4: ``_MAX_PERIODS`` is a sanity ceiling on the period COUNT, analogous to
    ``_MAX_ITERATIONS`` on the iteration count -- a runaway/typo'd period list (e.g. one entry
    per day for decades instead of one per month) must raise, not silently run an
    O(periods * iterations) simulation with no upper bound."""
    params = _params(
        periods=[{"expected_net": 1, "uncertainty_pct": 0}] * 1001,
        iterations=1,
    )
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, params)


def test_forecast_accepts_periods_at_the_max_periods_boundary() -> None:
    """The boundary itself must not move: exactly ``_MAX_PERIODS`` (1000) periods is valid."""
    params = _params(
        periods=[{"expected_net": 1, "uncertainty_pct": 0}] * 1000,
        iterations=1,
    )
    result = do_forecast_cashflow(1, params)
    assert len(result["periods"]) == 1000


def test_forecast_requires_expected_net() -> None:
    params = _params()
    del params["periods"][0]["expected_net"]
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, params)


@pytest.mark.parametrize("bad_amount", [True, float("nan"), float("inf"), "1000", None, [1]])
def test_forecast_rejects_bad_expected_net(bad_amount: Any) -> None:
    params = _params()
    params["periods"][0]["expected_net"] = bad_amount
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, params)


@pytest.mark.parametrize("bad_uncertainty", [-0.01, 5.01, True, float("nan"), float("inf"), "0.1"])
def test_forecast_rejects_bad_uncertainty(bad_uncertainty: Any) -> None:
    params = _params()
    params["periods"][0]["uncertainty_pct"] = bad_uncertainty
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, params)


def test_forecast_uncertainty_defaults_to_zero_when_absent() -> None:
    params = _params()
    del params["periods"][0]["uncertainty_pct"]
    result = do_forecast_cashflow(1, params)
    first = result["periods"][0]
    assert first["net_p10"] == first["net_p50"] == first["net_p90"]


def test_forecast_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, _params(unexpected_key=True))


def test_forecast_rejects_unknown_period_key() -> None:
    params = _params()
    params["periods"][0]["typo_field"] = 1
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, params)


def test_forecast_rejects_non_dict_params() -> None:
    with pytest.raises(ValueError):
        do_forecast_cashflow(1, "not-a-dict")  # type: ignore[arg-type]


# ===========================================================================
# (b) DUNNING — do_compute_dunning
# ===========================================================================


def _customer(risk_score: Any, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"credit_risk_score": risk_score}
    payload.update(overrides)
    return payload


# --- tier boundaries, pinned exactly (money-module briefing #5) -----------


@pytest.mark.parametrize(
    ("risk_score", "expected_tier"),
    [
        (0, "LOW"),
        (19, "LOW"),
        (19.999, "LOW"),
        (20, "STANDARD"),
        (39, "STANDARD"),
        (39.999, "STANDARD"),
        (40, "ELEVATED"),
        (59, "ELEVATED"),
        (60, "ELEVATED"),  # exactly the escalation boundary: stays ELEVATED, not escalated
        (60.0001, "CRITICAL"),  # the smallest step above it: escalates
        (61, "CRITICAL"),
        (100, "CRITICAL"),
    ],
)
def test_dunning_tier_boundaries(risk_score: Any, expected_tier: str) -> None:
    result = do_compute_dunning(_customer(risk_score))
    assert result["tier"] == expected_tier


def test_dunning_risk_exactly_60_is_not_escalated() -> None:
    """Pins the roadmap's exact wording: 'risk>60' is strictly greater-than, so 60 itself must
    NOT require HW-signing or a Lindorff handoff."""
    result = do_compute_dunning(_customer(60))
    assert result["tier"] == "ELEVATED"
    assert result["hw_signing_required"] is False
    assert result["lindorff_handoff"] is False


def test_dunning_risk_just_above_60_is_escalated() -> None:
    result = do_compute_dunning(_customer(60.0001))
    assert result["tier"] == "CRITICAL"
    assert result["hw_signing_required"] is True
    assert result["lindorff_handoff"] is True


def test_dunning_boundary_100_is_valid_and_critical() -> None:
    result = do_compute_dunning(_customer(100))
    assert result["tier"] == "CRITICAL"
    assert result["hw_signing_required"] is True


def test_dunning_boundary_zero_is_valid_and_low() -> None:
    result = do_compute_dunning(_customer(0))
    assert result["tier"] == "LOW"
    assert result["hw_signing_required"] is False


# --- FIX 2: Decimal risk score must not lose precision via a float() round-trip ------------


def test_dunning_decimal_just_above_60_by_1e_minus_16_is_critical() -> None:
    """FIX 2 (BLOCKING-ish): ``Decimal('60.0000000000000001')`` is genuinely > 60, but
    ``float(Decimal('60.0000000000000001'))`` round-trips to exactly ``60.0`` -- if the
    comparison were still done in float space (the pre-fix bug), this would silently
    under-escalate to ELEVATED with no HW-signing and no Lindorff handoff. The comparison must
    happen in Decimal space, where this value is unambiguously > 60."""
    result = do_compute_dunning(_customer(Decimal("60.0000000000000001")))
    assert result["tier"] == "CRITICAL"
    assert result["hw_signing_required"] is True
    assert result["lindorff_handoff"] is True


def test_dunning_decimal_exactly_60_stays_elevated() -> None:
    """Complement to the test above: pins that the boundary itself has not moved. A ``Decimal``
    risk score of exactly 60 (not 60.0000000000000001) must still stay ELEVATED, not escalate."""
    result = do_compute_dunning(_customer(Decimal("60")))
    assert result["tier"] == "ELEVATED"
    assert result["hw_signing_required"] is False
    assert result["lindorff_handoff"] is False


# --- FIX 3: OverflowError must not escape the documented ValueError contract ---------------


def test_dunning_huge_int_risk_score_raises_valueerror_not_overflowerror() -> None:
    """FIX 3: ``float(10**400)`` raises ``OverflowError`` (a plain int above ~1.8e308), which
    is NOT the ``ValueError`` this module's docstring documents as its only failure mode. This
    must be caught and re-raised as ``ValueError``, mirroring how forecast.py wraps
    ``DecimalException``."""
    with pytest.raises(ValueError):
        do_compute_dunning({"credit_risk_score": 10**400})


# --- reminder schedule -------------------------------------------------------


def test_dunning_low_tier_schedule_skips_early_reminders() -> None:
    result = do_compute_dunning(_customer(5))
    assert result["reminder_days"] == [10, 21]


@pytest.mark.parametrize("risk_score", [20, 50, 90])
def test_dunning_non_low_tiers_use_full_canonical_schedule(risk_score: Any) -> None:
    result = do_compute_dunning(_customer(risk_score))
    assert result["reminder_days"] == [-3, 3, 10, 21]


# --- customer_id echo ---------------------------------------------------------


def test_dunning_echoes_customer_id() -> None:
    result = do_compute_dunning(_customer(10, customer_id="cust-42"))
    assert result["customer_id"] == "cust-42"


def test_dunning_customer_id_defaults_to_none() -> None:
    result = do_compute_dunning(_customer(10))
    assert result["customer_id"] is None


def test_dunning_accepts_decimal_risk_score() -> None:
    result = do_compute_dunning(_customer(Decimal("45.5")))
    assert result["tier"] == "ELEVATED"
    assert result["risk_score"] == 45.5


# --- fail-toward-refusal for a missing/unknown/negative signal (briefing #4/#5) --------


@pytest.mark.parametrize(
    "bad_score",
    [
        None,  # missing bureau data — must not silently pick LOW or CRITICAL
        True,
        False,
        "60",  # never parsed
        float("nan"),
        float("inf"),
        float("-inf"),
        -1,
        -0.01,
        100.01,
        101,
        [60],
        {"score": 60},
    ],
)
def test_dunning_rejects_bad_risk_score(bad_score: Any) -> None:
    with pytest.raises(ValueError):
        do_compute_dunning(_customer(bad_score))


def test_dunning_missing_credit_risk_score_key_raises() -> None:
    with pytest.raises(ValueError):
        do_compute_dunning({"customer_id": "x"})


def test_dunning_rejects_unknown_customer_key() -> None:
    with pytest.raises(ValueError):
        do_compute_dunning({"credit_risk_score": 10, "typo_field": 1})


def test_dunning_rejects_non_dict_customer() -> None:
    with pytest.raises(ValueError):
        do_compute_dunning("not-a-dict")  # type: ignore[arg-type]


def test_dunning_reasons_mentions_tier() -> None:
    result = do_compute_dunning(_customer(10))
    assert result["tier"] in result["reasons"][0]
