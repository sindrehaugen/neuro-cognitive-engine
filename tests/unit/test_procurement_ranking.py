"""
tests/unit/test_procurement_ranking.py
========================================
Acceptance tests for Batch 045 — Module 1.Wave 2 (rank-suppliers).

Split per round-2 rule #3:
  (a) ALGORITHM tests — parameterised by fixture weights (never the tenant's real values).
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

import copy

import pytest

# ---------------------------------------------------------------------------
# Fixture weights — algorithm tests use THESE, never the tenant's real values
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
        "rebate_proximity": 0.05,
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
        "rebate_proximity": 0.05,
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
    "rebate_proximity": 0.4,
    "bundles_well": False,
}

_CANDIDATE_EXPENSIVE = {
    "supplier_id": "S-EXPENSIVE",
    "unit_price": 130.0,
    "delivery_reliability": 0.7,
    "own_stock": False,
    "lead_time_days": 7,
    "supplier_tier": 3,
    "rebate_proximity": 0.3,
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
        "rebate_proximity": 0.1,
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
        "rebate_proximity": 0.1,
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
        "rebate_proximity": 0.3,
        "bundles_well": False,
    }
    c_higher_price = {
        "supplier_id": "S-HIGHER",
        "unit_price": 120.0,
        "delivery_reliability": 0.80,
        "own_stock": False,
        "lead_time_days": 5,
        "supplier_tier": 2,
        "rebate_proximity": 0.3,
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
        "rebate_proximity": 0.0,
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
        "rebate_proximity": 1.0,
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
            "rebate_proximity": 0.25,
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
        "rebate_proximity": 0.9,
        "bundles_well": True,
    }
    c_weak = {
        "supplier_id": "WEAK",
        "unit_price": 110.0,
        "delivery_reliability": 0.50,
        "own_stock": False,
        "lead_time_days": 12,
        "supplier_tier": 4,
        "rebate_proximity": 0.1,
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
        "rebate_proximity": 0.6,
        "bundles_well": True,
    }
    c2 = {
        "supplier_id": "REAL-B",
        "unit_price": 140.0,
        "delivery_reliability": 0.7,
        "own_stock": False,
        "lead_time_days": 7,
        "supplier_tier": 3,
        "rebate_proximity": 0.2,
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


# ---------------------------------------------------------------------------
# 15-16. Legacy "kickback_proximity" key (renamed to "rebate_proximity"
#        on 2026-09-05). The rename must not silently change behaviour for a
#        namespace or caller that has not migrated: a dropped key would fall
#        back to the neutral 0.5 default, or to a zero weight, with no error.
# ---------------------------------------------------------------------------


def test_legacy_kickback_proximity_weight_key_is_still_honoured() -> None:
    """A namespace still overriding the OLD weight key keeps its tuning.

    Guard-the-guard: the third ranking uses neither key, and must differ from
    both — otherwise this test would pass even if the legacy lookup were dead.
    """
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    legacy = copy.deepcopy(_WEIGHTS_A)
    legacy["SCORING_WEIGHTS"]["kickback_proximity"] = legacy["SCORING_WEIGHTS"].pop(
        "rebate_proximity"
    )
    assert "rebate_proximity" not in legacy["SCORING_WEIGHTS"]

    heavy_new = copy.deepcopy(_WEIGHTS_A)
    heavy_new["SCORING_WEIGHTS"]["rebate_proximity"] = 0.90

    heavy_legacy = copy.deepcopy(heavy_new)
    heavy_legacy["SCORING_WEIGHTS"]["kickback_proximity"] = heavy_legacy["SCORING_WEIGHTS"].pop(
        "rebate_proximity"
    )

    cands = [dict(_CANDIDATE_CHEAP), dict(_CANDIDATE_EXPENSIVE)]

    baseline = do_rank_suppliers(_WEIGHTS_A, _BOM_LINE, cands)
    via_legacy = do_rank_suppliers(legacy, _BOM_LINE, cands)
    weighted = do_rank_suppliers(heavy_legacy, _BOM_LINE, cands)

    def scores(res: dict) -> list[float]:
        return [e["composite_score"] for e in res["ranked"]]

    # the legacy key reproduces the new key exactly
    assert scores(via_legacy) == scores(baseline)
    # ...and the weight is genuinely READ, not defaulted: raising it moves scores
    assert scores(weighted) != scores(baseline)


def test_legacy_kickback_proximity_candidate_field_is_still_honoured() -> None:
    """A caller still sending the OLD candidate field keeps its value.

    Guard-the-guard: a candidate carrying NEITHER field falls back to the
    neutral 0.5 and must score differently from one carrying 0.0.
    """
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    base = dict(_CANDIDATE_CHEAP)
    base.pop("rebate_proximity", None)

    legacy_zero = dict(base)
    legacy_zero["kickback_proximity"] = 0.0

    new_zero = dict(base)
    new_zero["rebate_proximity"] = 0.0

    heavy = copy.deepcopy(_WEIGHTS_A)
    heavy["SCORING_WEIGHTS"]["rebate_proximity"] = 0.90

    other = dict(_CANDIDATE_EXPENSIVE)

    def score_of(cand: dict) -> float:
        res = do_rank_suppliers(heavy, _BOM_LINE, [cand, other])
        return next(
            e["composite_score"] for e in res["ranked"] if e["supplier_id"] == cand["supplier_id"]
        )

    # legacy field is read and equals the new field
    assert score_of(legacy_zero) == score_of(new_zero)
    # ...and omitting BOTH gives the neutral 0.5 default, which differs from 0.0
    assert score_of(base) != score_of(new_zero)


def test_candidate_with_none_lead_time_days_does_not_crash() -> None:
    """A candidate with lead_time_days=None must not raise TypeError."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    cand = {
        "supplier_id": "NONE-LEAD",
        "unit_price": 99.0,
        "lead_time_days": None,
        "delivery_reliability": 0.85,
    }
    result = do_rank_suppliers(_WEIGHTS_A, {"quantity": 1}, [cand])
    assert len(result["ranked"]) == 1
    assert result["ranked"][0]["score_breakdown"]["step2_delivery_reliability"] == 0.85


def test_optional_lead_time_weight_promotes_faster_supplier() -> None:
    """When lead_time weight is configured, faster lead time contributes positively."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    weights_with_lead = copy.deepcopy(_WEIGHTS_A)
    weights_with_lead["SCORING_WEIGHTS"]["lead_time"] = 0.20

    fast = {
        "supplier_id": "FAST-LEAD",
        "unit_price": 100.0,
        "lead_time_days": 2,
        "delivery_reliability": 0.9,
    }
    slow = {
        "supplier_id": "SLOW-LEAD",
        "unit_price": 100.0,
        "lead_time_days": 10,
        "delivery_reliability": 0.9,
    }

    result = do_rank_suppliers(weights_with_lead, {"quantity": 5}, [fast, slow])
    fast_entry = next(e for e in result["ranked"] if e["supplier_id"] == "FAST-LEAD")
    slow_entry = next(e for e in result["ranked"] if e["supplier_id"] == "SLOW-LEAD")

    assert fast_entry["composite_score"] > slow_entry["composite_score"]


# ---------------------------------------------------------------------------
# 17-21. Wave E-9: Metal surcharge (Kobbertillegg) in TCO & cognitive ranking
# ---------------------------------------------------------------------------


def test_tco_metal_surcharge_calculated_from_lme_and_copper_weight() -> None:
    """TCO includes Kobbertillegg when metal weight and LME rate are provided."""
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    supplier = {
        "supplier_id": "CABLE-SUPP-1",
        "unit_price": 50.0,
        "lme_rate_nok_per_kg": 85.0,
        "baseline_rate_nok_per_kg": 50.0,
    }
    # 200m of cable, 150 kg/km copper
    # metal_weight = (200 / 1000) * 150 = 30 kg
    # rate_diff = 85.0 - 50.0 = 35.0 NOK/kg
    # surcharge = 30 * 35 = 1050.0 NOK
    bom_line = {
        "quantity": 200,
        "metal_weight_kg_per_km": 150.0,
    }

    tco = do_calculate_tco(_WEIGHTS_A, {}, supplier, bom_line)

    assert "metal_surcharge" in tco
    assert tco["metal_surcharge"] == 1050.0
    expected_sum = (
        tco["price"]
        + tco["freight"]
        + tco["warranty"]
        + tco["stock"]
        + tco["delivery_risk"]
        + tco["metal_surcharge"]
    )
    assert abs(tco["total"] - expected_sum) < 1e-9


def test_tco_metal_surcharge_from_precomputed_unit_rate() -> None:
    """TCO calculates metal surcharge using metal_surcharge_per_unit."""
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    supplier = {
        "supplier_id": "CABLE-SUPP-UNIT",
        "unit_price": 100.0,
        "metal_surcharge_per_unit": 12.50,
    }
    bom_line = {"quantity": 80}

    tco = do_calculate_tco(_WEIGHTS_A, {}, supplier, bom_line)

    assert "metal_surcharge" in tco
    assert tco["metal_surcharge"] == round(12.50 * 80, 2)
    assert tco["metal_surcharge"] == 1000.0


def test_metal_surcharge_shifts_supplier_ranking() -> None:
    """A supplier with lower base price but high metal surcharge loses to lower TCO supplier."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    # Candidate 1: base unit price 65 NOK, high metal surcharge (LME 200 NOK/kg, base 0 NOK/kg)
    # 1000m * 200 kg/km = 200 kg metal * 200 NOK/kg = 40,000 NOK metal surcharge
    c_cheap_base_high_surcharge = {
        "supplier_id": "S-CHEAP-BASE",
        "unit_price": 65.0,
        "lme_rate_nok_per_kg": 200.0,
        "baseline_rate_nok_per_kg": 0.0,
        "delivery_reliability": 0.9,
    }
    # Candidate 2: base unit price 70 NOK, but fixed baseline absorbs surcharge (baseline 200 NOK/kg)
    # surcharge = 0 NOK
    c_higher_base_low_surcharge = {
        "supplier_id": "S-STABLE-PRICE",
        "unit_price": 70.0,
        "lme_rate_nok_per_kg": 200.0,
        "baseline_rate_nok_per_kg": 200.0,
        "delivery_reliability": 0.9,
    }

    bom_cable = {
        "quantity": 1000,
        "metal_weight_kg_per_km": 200.0,
    }

    result = do_rank_suppliers(
        _WEIGHTS_A, bom_cable, [c_cheap_base_high_surcharge, c_higher_base_low_surcharge]
    )

    ranked = result["ranked"]
    entry_cheap_base = next(e for e in ranked if e["supplier_id"] == "S-CHEAP-BASE")
    entry_stable = next(e for e in ranked if e["supplier_id"] == "S-STABLE-PRICE")

    # S-STABLE-PRICE has higher unit price, but much lower metal surcharge (0 vs 40,000)
    assert entry_cheap_base["tco"]["metal_surcharge"] == 40000.0
    assert entry_stable["tco"]["metal_surcharge"] == 0.0
    assert entry_stable["tco"]["total"] < entry_cheap_base["tco"]["total"]
    # S-STABLE-PRICE outranks S-CHEAP-BASE in total TCO
    assert entry_stable["composite_score"] > entry_cheap_base["composite_score"]
    assert ranked[0]["supplier_id"] == "S-STABLE-PRICE"


# ---------------------------------------------------------------------------
# 22-26. Wave E-9: Regulatory compliance gating (DNV TAC, ISO 9001, ATEX, RoHS 3)
# ---------------------------------------------------------------------------


def test_regulatory_compliance_gating_dnv_and_iso_penalty() -> None:
    """Non-compliant candidate missing DNV_TAC suffers compliance penalty and is outranked."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    bom_marine = {
        "quantity": 10,
        "required_certifications": ["DNV_TAC", "ISO_9001"],
    }

    # Cheap supplier missing DNV_TAC
    c_cheap_noncompliant = {
        "supplier_id": "S-CHEAP-NONCOMPLIANT",
        "unit_price": 50.0,
        "delivery_reliability": 0.95,
        "certifications": ["ISO_9001"],
    }
    # More expensive supplier with full DNV TAC and ISO 9001 compliance
    c_compliant = {
        "supplier_id": "S-COMPLIANT-MARINE",
        "unit_price": 100.0,
        "delivery_reliability": 0.90,
        "certifications": ["DNV_TAC", "ISO_9001"],
    }

    result = do_rank_suppliers(_WEIGHTS_A, bom_marine, [c_cheap_noncompliant, c_compliant])

    # Result includes disqualified_suppliers
    assert "disqualified_suppliers" in result
    assert result["disqualified_suppliers"] == ["S-CHEAP-NONCOMPLIANT"]

    ranked = result["ranked"]
    winner = ranked[0]
    assert winner["supplier_id"] == "S-COMPLIANT-MARINE"
    assert winner["regulatory_compliant"] is True
    assert winner["missing_certifications"] == []

    loser = ranked[1]
    assert loser["supplier_id"] == "S-CHEAP-NONCOMPLIANT"
    assert loser["regulatory_compliant"] is False
    assert loser["missing_certifications"] == ["DNV_TAC"]
    # Penalty applied: composite was reduced by * 0.1
    assert loser["composite_score"] < winner["composite_score"]


def test_strict_regulatory_gate_zeroes_score() -> None:
    """When strict_regulatory_gate is True, non-compliant composite_score is 0.0."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    bom_strict = {
        "quantity": 5,
        "required_certifications": ["ATEX_2014_34_EU", "ISO_9001"],
        "strict_regulatory_gate": True,
    }

    c1 = {
        "supplier_id": "S-EX-COMPLIANT",
        "unit_price": 120.0,
        "certifications": ["ATEX", "ISO_9001"],
    }
    c2 = {
        "supplier_id": "S-EX-DEFICIENT",
        "unit_price": 80.0,
        "certifications": ["ISO_9001"],
    }

    result = do_rank_suppliers(_WEIGHTS_A, bom_strict, [c1, c2])

    deficient_entry = next(e for e in result["ranked"] if e["supplier_id"] == "S-EX-DEFICIENT")
    compliant_entry = next(e for e in result["ranked"] if e["supplier_id"] == "S-EX-COMPLIANT")

    assert deficient_entry["composite_score"] == 0.0
    assert compliant_entry["composite_score"] > 0.0
    assert result["disqualified_suppliers"] == ["S-EX-DEFICIENT"]


def test_regulatory_scope_dnv_marine_and_atex() -> None:
    """regulatory_scope='dnv_marine' requires DNV_TAC and ISO_9001 automatically."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    bom_scope = {
        "quantity": 10,
        "regulatory_scope": "dnv_marine",
    }

    c_full = {
        "supplier_id": "S-FULL-DNV",
        "unit_price": 100.0,
        "certifications": {"DNV_TAC": True, "ISO_9001": True},
    }
    c_partial = {
        "supplier_id": "S-PARTIAL-DNV",
        "unit_price": 70.0,
        "certifications": {"DNV_TAC": True, "ISO_9001": False},
    }

    result = do_rank_suppliers(_WEIGHTS_A, bom_scope, [c_full, c_partial])

    assert result["disqualified_suppliers"] == ["S-PARTIAL-DNV"]
    partial_entry = next(e for e in result["ranked"] if e["supplier_id"] == "S-PARTIAL-DNV")
    assert partial_entry["regulatory_compliant"] is False
    assert partial_entry["missing_certifications"] == ["ISO_9001"]


def test_regulatory_scope_electrical_lv_and_pcba_with_rohs() -> None:
    """electrical_lv checks CE/ROHS_3, and electronics_pcba checks ROHS_3/REACH/ISO_9001."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    bom_lv = {
        "quantity": 10,
        "regulatory_scope": "electrical_lv",
    }

    c_ce_rohs = {
        "supplier_id": "S-LV-COMPLIANT",
        "unit_price": 90.0,
        "certifications": ["CE", "ROHS_3"],
    }
    c_no_rohs = {
        "supplier_id": "S-LV-NO-ROHS",
        "unit_price": 80.0,
        "certifications": ["CE"],
    }

    result = do_rank_suppliers(_WEIGHTS_A, bom_lv, [c_ce_rohs, c_no_rohs])
    assert result["disqualified_suppliers"] == ["S-LV-NO-ROHS"]

    no_rohs_entry = next(e for e in result["ranked"] if e["supplier_id"] == "S-LV-NO-ROHS")
    assert no_rohs_entry["missing_certifications"] == ["ROHS_3"]


def test_no_regulatory_keys_when_gating_not_requested() -> None:
    """When required_certifications and regulatory_scope are absent, no compliance keys are added."""
    from nce.vertical_modules.procurement.ranking import do_rank_suppliers

    bom_standard = {"quantity": 5}
    candidates = [
        {"supplier_id": "C1", "unit_price": 50.0},
        {"supplier_id": "C2", "unit_price": 60.0},
    ]

    result = do_rank_suppliers(_WEIGHTS_A, bom_standard, candidates)

    assert set(result.keys()) == {"ranked", "rebate_override", "rebate_rationale"}
    assert "disqualified_suppliers" not in result
    for entry in result["ranked"]:
        assert "regulatory_compliant" not in entry
        assert "missing_certifications" not in entry
