"""Unit regression cover for the rebate-ceiling derivation in compliance.py.

These are deliberately UNIT tests (no @pytest.mark.integration): the ceiling is a
pure function, and the CI unit job runs `-m "not integration and not perf"`, so
integration-marked tests never gate a merge. The §9.3 money gate deserves a gate
that actually runs.

Regression context — `_evaluate_discount_limit` used to derive the ceiling from
the GLOBAL MAXIMUM tier pct, ignoring whether the committed volume ever reached
that tier:

    rates.append(max(Decimal(str(tier["pct"])) for tier in tiers))

With tiers [100k@2%, 10M@25%] and volumeCommitment 200k, the engine's own
retroactive-on-total model (kickback._tier_progression) earns 200k x 2% = 4,000,
while the old ceiling authorised 200k x 25% = 50,000 — a >10x over-approval in
the gate procurement's rebate_override depends on.

Every pre-existing test in tests/test_agreements_compliance.py sets
_VOLUME_COMMITMENT (1_000_000.0) exactly equal to _TIERS_3's top threshold
(1_000_000.0). At that volume the top tier IS the active tier, so the buggy and
correct formulas coincide — the suite was structurally blind. The tests below
therefore all use a volume STRICTLY BELOW the top tier.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nce.vertical_modules.agreements.compliance import _evaluate_discount_limit

# The audit's reproduction case: a wide gap between a reached and an unreached tier.
_TIERS_GAPPED: list[dict[str, float]] = [
    {"threshold": 100_000.0, "pct": 2.0},
    {"threshold": 10_000_000.0, "pct": 25.0},
]

# The project's standard 3-tier table (mirrors tests/test_agreements_compliance.py).
_TIERS_3: list[dict[str, float]] = [
    {"threshold": 100_000.0, "pct": 2.0},
    {"threshold": 500_000.0, "pct": 3.5},
    {"threshold": 1_000_000.0, "pct": 5.0},
]


def _terms(tiers=None, volume=None, frame=None) -> dict:
    t: dict = {}
    if tiers is not None:
        t["kickbackTiers"] = tiers
    if volume is not None:
        t["volumeCommitment"] = volume
    if frame is not None:
        t["frameDiscountPct"] = frame
    return t


class TestActiveTierCeiling:
    """The ceiling must come from the tier actually reached, not the global max."""

    def test_unreached_top_tier_does_not_raise_the_ceiling(self):
        # Active tier at 200k is 100k@2% -> ceiling 4,000. The old global-max
        # behaviour would have allowed up to 50,000.
        terms = _terms(_TIERS_GAPPED, volume=200_000.0)
        ok, reason = _evaluate_discount_limit(terms, Decimal("4000"))
        assert ok is True, reason

        for over in ("4000.01", "10000", "45000", "50000"):
            ok, reason = _evaluate_discount_limit(terms, Decimal(over))
            assert ok is False, f"rebate {over} must be denied at a 4,000 ceiling"
            assert "exceeds signed ceiling" in reason

    def test_the_exact_audit_repro_is_denied(self):
        # Was APPROVED before the fix.
        ok, reason = _evaluate_discount_limit(
            _terms(_TIERS_GAPPED, volume=200_000.0), Decimal("45000")
        )
        assert ok is False
        assert "4000.00" in reason

    def test_middle_tier_active_on_three_tier_table(self):
        # 600k reaches 500k@3.5% but not 1M@5% -> ceiling 21,000 (not 30,000).
        terms = _terms(_TIERS_3, volume=600_000.0)
        ok, _ = _evaluate_discount_limit(terms, Decimal("21000"))
        assert ok is True
        ok, reason = _evaluate_discount_limit(terms, Decimal("21000.01"))
        assert ok is False
        # 30,000 is what the old global-max (5.0%) ceiling would have allowed.
        ok, _ = _evaluate_discount_limit(terms, Decimal("30000"))
        assert ok is False

    def test_threshold_boundary_is_inclusive(self):
        # Exactly at 500k the 3.5% tier is active -> 17,500.
        terms = _terms(_TIERS_3, volume=500_000.0)
        ok, _ = _evaluate_discount_limit(terms, Decimal("17500"))
        assert ok is True
        ok, _ = _evaluate_discount_limit(terms, Decimal("17500.01"))
        assert ok is False

    def test_top_tier_still_applies_when_actually_reached(self):
        # Guards against over-correcting: at/above the top threshold the top
        # tier is genuinely active. (This is the only case the old suite covered.)
        ok, _ = _evaluate_discount_limit(_terms(_TIERS_3, volume=1_000_000.0), Decimal("50000"))
        assert ok is True
        ok, _ = _evaluate_discount_limit(_terms(_TIERS_3, volume=2_000_000.0), Decimal("100000"))
        assert ok is True


class TestNoReachableBasis:
    """Below the first threshold the entitlement is zero — fail closed."""

    def test_volume_below_first_tier_denies_any_positive_rebate(self):
        terms = _terms(_TIERS_3, volume=50_000.0)
        ok, reason = _evaluate_discount_limit(terms, Decimal("1000"))
        assert ok is False
        assert "reaches no kickback tier" in reason

    def test_frame_discount_still_applies_below_first_tier(self):
        # A frame discount is an independent basis: 50k x 1% = 500.
        terms = _terms(_TIERS_3, volume=50_000.0, frame=1.0)
        ok, _ = _evaluate_discount_limit(terms, Decimal("500"))
        assert ok is True
        ok, _ = _evaluate_discount_limit(terms, Decimal("500.01"))
        assert ok is False


class TestFrameDiscountInteraction:
    def test_higher_frame_rate_wins_over_active_tier(self):
        # Active tier 2% (4,000) vs frame 10% (20,000) -> the more generous
        # independent basis governs.
        terms = _terms(_TIERS_GAPPED, volume=200_000.0, frame=10.0)
        ok, _ = _evaluate_discount_limit(terms, Decimal("20000"))
        assert ok is True
        ok, _ = _evaluate_discount_limit(terms, Decimal("20000.01"))
        assert ok is False

    def test_unreached_tier_does_not_win_over_frame(self):
        # The 25% tier is unreached; frame 10% governs, so 50,000 stays denied.
        ok, _ = _evaluate_discount_limit(
            _terms(_TIERS_GAPPED, volume=200_000.0, frame=10.0), Decimal("50000")
        )
        assert ok is False


class TestFailClosedPreserved:
    """Pre-existing fail-closed behaviour must survive the fix."""

    def test_no_provision_at_all(self):
        ok, reason = _evaluate_discount_limit(_terms(volume=200_000.0), Decimal("1"))
        assert ok is False
        assert "no signed rebate or kickback provision" in reason

    def test_no_volume_basis(self):
        ok, reason = _evaluate_discount_limit(_terms(_TIERS_3), Decimal("1"))
        assert ok is False
        assert "volumeCommitment" in reason

    @pytest.mark.parametrize(
        "bad_tiers",
        [
            [{"threshold": float("nan"), "pct": 2.0}],
            [{"threshold": 100.0, "pct": float("inf")}],
            [{"threshold": -100.0, "pct": 2.0}],
            [{"threshold": 100.0, "pct": True}],
            [{"threshold": 100.0}],
            "not-a-list",
        ],
    )
    def test_malformed_tiers_fail_closed(self, bad_tiers):
        ok, reason = _evaluate_discount_limit(_terms(bad_tiers, volume=200_000.0), Decimal("1"))
        assert ok is False
        assert "malformed signed terms" in reason

    def test_zero_rebate_is_allowed_when_a_basis_exists(self):
        ok, _ = _evaluate_discount_limit(_terms(_TIERS_3, volume=600_000.0), Decimal("0"))
        assert ok is True
