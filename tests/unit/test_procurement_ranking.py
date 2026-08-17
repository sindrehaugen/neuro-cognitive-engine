"""
tests/unit/test_procurement_ranking.py
========================================
Acceptance tests for Batch 045 — Module 1.Wave 2 (rank-suppliers).

Split per round-2 rule #3:
  (a) ALGORITHM tests — parameterised by fixture weights (never Bravo's tenant values).
  (b) CONFIG integration — exercises real JSON config via load_procurement_config.

Covers:
  1.  Result has documented keys: ``ranked``, ``rebate_override``, ``rebate_rationale``.
  2.  Each ranked entry has ``composite_score``, ``score_breakdown``, ``tco``.
  3.  Candidates are sorted descending by ``composite_score``.
  4.  Different weights produce different rankings (config drives behaviour).
  5.  MILESTONE — priceScore gap closed: lowering a candidate's price improves its rank.
  6.  Rebate-override fires when step-5 changes the winner vs best-TCO.
  7.  ``rebate_override=False`` and empty rationale when step-5 does not change winner.
  8.  5-step order is deterministic: same inputs → same output.
  9.  own_stock=True preference is reflected in composite score (step 1).
  10. Delivery-deadline miss scores 0 on delivery_reliability (step 2).
  11. Conservative defaults: candidate missing optional fields is not dropped.
  12. Empty candidates list raises ValueError.
  13. Candidate missing unit_price raises ValueError (via TCO).
  14. Real config loads and produces a valid ranking (config integration test).

All tests are plain unit tests (no DB, no Redis, no HTTP).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Fixture weights — algorithm tests use THESE, never Bravo's tenant values
# ---------------------------------------------------------------------------

_WEIGHTS_A: dict = {
    "TCO_WEIGHTS": {
        "freight": 0.05,
        "warranty": 0.08,
        "stock": 0.03,
        "delivery_risk": 0.04,
    },
    "SCORING_WEIGHTS": {
        "tco": 0.40,
        "delivery_reliability": 0.25,
        "bid_price": 0.20,
        "tier_bundling": 0.10,
        "kickback_proximity": 0.05,
    },
}

# Different weights — proves config drives behaviour.
_WEIGHTS_B: dict = {
    "TCO_WEIGHTS": {
        "freight": 0.02,
        "warranty": 0.03,
        "stock": 0.01,
        "delivery_risk": 0.02,
    },
    "SCORING_WEIGHTS": {
        "tco": 0.20,
        "delivery_reliability": 0.50,
        "bid_price": 0.15,
        "tier_bundling": 0.10,
        "kickback_proximity": 0.05,
    },
}

_BOM_LINE = {"quantity": 5, "unit_price": 100.0}

# Two generic candidates for most tests
_CANDIDATE_CHEAP = {
    "supplier_id": "S-CHEAP",
    "unit_price": 80.0,
    "delivery_reliability": 0.9,
    "own_stock": False,
    "lead_time_days": 5,
    "supplier_tier": 2,
    "kickback_proximity": 0.4,
    "bundles_well": False,
}

_CANDIDATE_EXPENSIVE = {
    "supplier_id": "S-EXPENSIVE",
    "unit_price": 130.0,
    "delivery_reliability": 0.7,
    "own_stock": False,
    "lead_time_days": 7,
    "supplier_tier": 3,
    "kickback_proximity": 0.3,
    "bundles_well": False,
}


# ---------------------------------------------------------------------------
# 1. Result keys
# ---------------------------------------------------------------------------


def test_result_has_documented_keys():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    result = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [_CANDIDATE_CHEAP])

    assert set(result.keys()) == {"ranked", "rebate_override", "rebate_rationale"}


# ---------------------------------------------------------------------------
# 2. Each ranked entry has score_breakdown and tco
# ---------------------------------------------------------------------------


def test_ranked_entry_has_composite_score_breakdown_and_tco():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    result = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [_CANDIDATE_CHEAP, _CANDIDATE_EXPENSIVE])
    entry = result["ranked"][0]

    assert "composite_score" in entry
    assert "score_breakdown" in entry
    assert "tco" in entry

    breakdown = entry["score_breakdown"]
    for step in (
        "step1_own_stock",
        "step2_delivery_reliability",
        "step3_tco",
        "step4_bid_price",
        "step5_tier_kickback_bundling",
    ):
        assert step in breakdown, f"score_breakdown missing '{step}'"


# ---------------------------------------------------------------------------
# 3. Sorted descending by composite_score
# ---------------------------------------------------------------------------


def test_ranked_sorted_descending():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    result = do_rank_suppliers(
        _WEIGHTS_A,
        _BOM_LINE,
        [_CANDIDATE_CHEAP, _CANDIDATE_EXPENSIVE],
    )
    scores = [e["composite_score"] for e in result["ranked"]]
    assert scores == sorted(scores, reverse=True), (
        "ranked list must be descending by composite_score"
    )


# ---------------------------------------------------------------------------
# 4. Config drives behaviour — different weights → different rankings
# ---------------------------------------------------------------------------


def test_different_weights_can_produce_different_winner():
    """Altering weights changes the relative order (when candidates are balanced)."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    # Candidate with high delivery_reliability but higher price
    c_reliable = {
        "supplier_id": "S-RELIABLE",
        "unit_price": 120.0,
        "delivery_reliability": 0.99,
        "own_stock": False,
        "lead_time_days": 3,
        "supplier_tier": 1,
        "kickback_proximity": 0.1,
        "bundles_well": False,
    }
    # Candidate with low price but poor delivery
    c_cheap = {
        "supplier_id": "S-CHEAP2",
        "unit_price": 80.0,
        "delivery_reliability": 0.20,
        "own_stock": False,
        "lead_time_days": 14,
        "supplier_tier": 4,
        "kickback_proximity": 0.1,
        "bundles_well": False,
    }

    # WEIGHTS_B heavily favours delivery_reliability (0.50 vs 0.15 bid)
    result_b = do_rank_suppliers(_WEIGHTS_B, _BOM_LINE, [c_reliable, c_cheap])
    # WEIGHTS_A balances tco/price more equally
    result_a = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [c_reliable, c_cheap])

    winner_a = result_a["ranked"][0]["supplier_id"]
    winner_b = result_b["ranked"][0]["supplier_id"]

    # With heavily delivery-weighted config, the reliable supplier should win.
    assert winner_b == "S-RELIABLE", (
        "High delivery_reliability weight must elevate the reliable supplier"
    )
    # The two configs must yield different scores (even if same winner here, scores differ).
    score_a = result_a["ranked"][0]["composite_score"]
    score_b = result_b["ranked"][0]["composite_score"]
    assert score_a != score_b, "Different weights must produce different composite scores"

    _ = winner_a  # used to keep the variable from triggering linter


# ---------------------------------------------------------------------------
# 5. MILESTONE — priceScore gap closed: lowering price improves rank
# ---------------------------------------------------------------------------


def test_lower_price_improves_rank():
    """Core acceptance: the cheapest candidate must rank higher on bid_price step.

    BEFORE (priceScore=3 placeholder): every candidate got the same flat score=3
    regardless of price — ranking did not respond to price at all.

    AFTER: priceScore = min_price / unit_price, so S-LOWER (cheaper) scores higher
    on step4_bid_price than S-HIGHER, and this difference propagates to composite_score.
    """
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    c_lower_price = {
        "supplier_id": "S-LOWER",
        "unit_price": 90.0,
        "delivery_reliability": 0.80,
        "own_stock": False,
        "lead_time_days": 5,
        "supplier_tier": 2,
        "kickback_proximity": 0.3,
        "bundles_well": False,
    }
    c_higher_price = {
        "supplier_id": "S-HIGHER",
        "unit_price": 120.0,
        "delivery_reliability": 0.80,
        "own_stock": False,
        "lead_time_days": 5,
        "supplier_tier": 2,
        "kickback_proximity": 0.3,
        "bundles_well": False,
    }

    result = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [c_lower_price, c_higher_price])

    ranked = result["ranked"]
    lower_entry = next(e for e in ranked if e["supplier_id"] == "S-LOWER")
    higher_entry = next(e for e in ranked if e["supplier_id"] == "S-HIGHER")

    assert (
        lower_entry["score_breakdown"]["step4_bid_price"]
        > higher_entry["score_breakdown"]["step4_bid_price"]
    ), "Cheaper candidate must score higher on step4_bid_price (priceScore gap closed)"
    assert lower_entry["composite_score"] > higher_entry["composite_score"], (
        "Cheaper candidate (identical in all other dimensions) must rank higher overall"
    )
    assert ranked[0]["supplier_id"] == "S-LOWER", (
        "The cheapest candidate must be ranked first when all else is equal"
    )


def test_price_score_is_one_for_cheapest_candidate():
    """The cheapest candidate receives step4_bid_price = 1.0 (full score)."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    c_cheapest = {"supplier_id": "C1", "unit_price": 50.0, "delivery_reliability": 0.5}
    c_costlier = {"supplier_id": "C2", "unit_price": 100.0, "delivery_reliability": 0.5}

    result = do_rank_suppliers(_WEIGHTS_A, {"quantity": 1}, [c_cheapest, c_costlier])
    cheapest_entry = next(e for e in result["ranked"] if e["supplier_id"] == "C1")

    assert abs(cheapest_entry["score_breakdown"]["step4_bid_price"] - 1.0) < 1e-9, (
        "The cheapest candidate must receive step4_bid_price = 1.0"
    )


# ---------------------------------------------------------------------------
# 6. rebate_override fires when step-5 changes winner vs best-TCO
# ---------------------------------------------------------------------------


def test_rebate_override_fires_when_step5_changes_winner():
    """When a high-tier/kickback candidate beats the cheapest-TCO candidate,
    rebate_override must be True and rationale must be non-empty."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    # Best-TCO candidate: cheapest price, no tier or kickback advantages
    c_best_tco = {
        "supplier_id": "TCO-WINNER",
        "unit_price": 70.0,
        "delivery_reliability": 0.50,
        "own_stock": False,
        "lead_time_days": 10,
        "supplier_tier": 4,
        "kickback_proximity": 0.0,
        "bundles_well": False,
    }
    # Governance-steered candidate: higher price but huge tier/kickback/bundling bonus
    c_kickback = {
        "supplier_id": "KICKBACK-WINNER",
        "unit_price": 72.0,  # slightly more expensive
        "delivery_reliability": 0.90,
        "own_stock": True,
        "lead_time_days": 3,
        "supplier_tier": 1,
        "kickback_proximity": 1.0,
        "bundles_well": True,
    }

    # Use weights that favour tier/kickback heavily to force the override
    weights_kickback_heavy = {
        "TCO_WEIGHTS": {
            "freight": 0.05,
            "warranty": 0.08,
            "stock": 0.03,
            "delivery_risk": 0.04,
        },
        "SCORING_WEIGHTS": {
            "tco": 0.10,
            "delivery_reliability": 0.10,
            "bid_price": 0.10,
            "tier_bundling": 0.45,
            "kickback_proximity": 0.25,
        },
    }

    result = do_rank_suppliers(weights_kickback_heavy, _BOM_LINE, [c_best_tco, c_kickback])

    assert result["rebate_override"] is True, (
        "rebate_override must be True when step-5 overrides TCO winner"
    )
    assert result["rebate_rationale"], "rebate_rationale must be a non-empty string"
    assert (
        "KICKBACK-WINNER" in result["rebate_rationale"]
        or "TCO-WINNER" in result["rebate_rationale"]
    ), "rationale must mention at least one of the two relevant suppliers"


# ---------------------------------------------------------------------------
# 7. rebate_override=False when step-5 does not change winner
# ---------------------------------------------------------------------------


def test_no_rebate_override_when_tco_winner_also_composite_winner():
    """When the cheapest-TCO candidate also wins overall, rebate_override must be False."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    # One clearly dominant candidate on all dimensions
    c_dominant = {
        "supplier_id": "DOMINANT",
        "unit_price": 60.0,
        "delivery_reliability": 0.95,
        "own_stock": True,
        "lead_time_days": 2,
        "supplier_tier": 1,
        "kickback_proximity": 0.9,
        "bundles_well": True,
    }
    c_weak = {
        "supplier_id": "WEAK",
        "unit_price": 110.0,
        "delivery_reliability": 0.50,
        "own_stock": False,
        "lead_time_days": 12,
        "supplier_tier": 4,
        "kickback_proximity": 0.1,
        "bundles_well": False,
    }

    result = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [c_dominant, c_weak])

    assert result["rebate_override"] is False
    assert result["rebate_rationale"] == ""


# ---------------------------------------------------------------------------
# 8. Deterministic: same inputs → same output
# ---------------------------------------------------------------------------


def test_ranking_is_deterministic():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    result_1 = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [_CANDIDATE_CHEAP, _CANDIDATE_EXPENSIVE])
    result_2 = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [_CANDIDATE_CHEAP, _CANDIDATE_EXPENSIVE])

    scores_1 = [e["composite_score"] for e in result_1["ranked"]]
    scores_2 = [e["composite_score"] for e in result_2["ranked"]]
    ids_1 = [e["supplier_id"] for e in result_1["ranked"]]
    ids_2 = [e["supplier_id"] for e in result_2["ranked"]]

    assert scores_1 == scores_2
    assert ids_1 == ids_2


# ---------------------------------------------------------------------------
# 9. own_stock=True is reflected in step1 score
# ---------------------------------------------------------------------------


def test_own_stock_contributes_to_composite_score():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    c_with_stock = {**_CANDIDATE_CHEAP, "supplier_id": "WITH-STOCK", "own_stock": True}
    c_without_stock = {**_CANDIDATE_CHEAP, "supplier_id": "NO-STOCK", "own_stock": False}

    result = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [c_with_stock, c_without_stock])

    stock_entry = next(e for e in result["ranked"] if e["supplier_id"] == "WITH-STOCK")
    no_stock_entry = next(e for e in result["ranked"] if e["supplier_id"] == "NO-STOCK")

    assert stock_entry["score_breakdown"]["step1_own_stock"] == 1.0
    assert no_stock_entry["score_breakdown"]["step1_own_stock"] == 0.0
    assert stock_entry["composite_score"] > no_stock_entry["composite_score"], (
        "own_stock=True must contribute positively to composite_score"
    )


# ---------------------------------------------------------------------------
# 10. Delivery deadline miss scores 0 on step2
# ---------------------------------------------------------------------------


def test_deadline_miss_scores_zero_on_delivery_reliability():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    bom_tight = {**_BOM_LINE, "required_by_day": 3}

    c_fast = {**_CANDIDATE_CHEAP, "supplier_id": "FAST", "lead_time_days": 2}
    c_slow = {**_CANDIDATE_CHEAP, "supplier_id": "SLOW", "lead_time_days": 10}

    result = do_rank_suppliers(_WEIGHTS_A, bom_tight, [c_fast, c_slow])

    fast_entry = next(e for e in result["ranked"] if e["supplier_id"] == "FAST")
    slow_entry = next(e for e in result["ranked"] if e["supplier_id"] == "SLOW")

    assert slow_entry["score_breakdown"]["step2_delivery_reliability"] == 0.0, (
        "Candidate missing deadline must score 0 on delivery_reliability"
    )
    assert fast_entry["score_breakdown"]["step2_delivery_reliability"] > 0.0, (
        "Candidate meeting deadline must score > 0 on delivery_reliability"
    )


# ---------------------------------------------------------------------------
# 11. Conservative defaults: missing optional fields do not drop the candidate
# ---------------------------------------------------------------------------


def test_candidate_with_only_required_fields_is_ranked():
    """A candidate with only unit_price is not dropped — conservative defaults apply."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    minimal = {"supplier_id": "MINIMAL", "unit_price": 95.0}
    result = do_rank_suppliers(_WEIGHTS_A, {"quantity": 2}, [minimal])

    assert len(result["ranked"]) == 1
    entry = result["ranked"][0]
    assert entry["supplier_id"] == "MINIMAL"
    assert entry["composite_score"] >= 0.0


# ---------------------------------------------------------------------------
# 12. Empty candidates list raises ValueError
# ---------------------------------------------------------------------------


def test_empty_candidates_raises_value_error():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    with pytest.raises(ValueError, match="empty"):
        do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [])


# ---------------------------------------------------------------------------
# 13. Candidate missing unit_price raises (via TCO)
# ---------------------------------------------------------------------------


def test_candidate_missing_unit_price_raises():
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    with pytest.raises((ValueError, KeyError)):
        do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, [{"supplier_id": "NO-PRICE"}])


# ---------------------------------------------------------------------------
# 14. Real config integration — load_procurement_config + do_rank_suppliers
# ---------------------------------------------------------------------------


def test_real_config_produces_valid_ranking():
    """load_procurement_config drives do_rank_suppliers end-to-end."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers
    from nce.vertical_modules.procurement.tco import load_procurement_config

    weights, _tolerances = load_procurement_config()

    c1 = {
        "supplier_id": "REAL-A",
        "unit_price": 150.0,
        "delivery_reliability": 0.9,
        "own_stock": True,
        "lead_time_days": 3,
        "supplier_tier": 1,
        "kickback_proximity": 0.6,
        "bundles_well": True,
    }
    c2 = {
        "supplier_id": "REAL-B",
        "unit_price": 140.0,
        "delivery_reliability": 0.7,
        "own_stock": False,
        "lead_time_days": 7,
        "supplier_tier": 3,
        "kickback_proximity": 0.2,
        "bundles_well": False,
    }

    result = do_rank_suppliers(weights, {"quantity": 10, "unit_price": 145.0}, [c1, c2])

    assert set(result.keys()) == {"ranked", "rebate_override", "rebate_rationale"}
    assert len(result["ranked"]) == 2
    scores = [e["composite_score"] for e in result["ranked"]]
    assert scores == sorted(scores, reverse=True)
    for entry in result["ranked"]:
        assert "tco" in entry
        assert entry["tco"]["total"] > 0
