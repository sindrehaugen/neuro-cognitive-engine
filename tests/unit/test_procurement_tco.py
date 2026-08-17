"""
tests/unit/test_procurement_tco.py
=====================================
Acceptance tests for Batch 044 — Module 1.Wave 1 (tco-core).

Split per round-2 rule #3:
  (a) ALGORITHM tests — parameterised by fixture weights injected directly; prove
      that the same algorithm with different weights yields different totals.
      No Bravo/tenant weight literals.
  (b) CONFIG tests — assert the two real JSON files load and contain all documented keys.

Covers:
  1. Result has exactly the 6 documented keys.
  2. ``total`` equals the sum of the five components.
  3. Config drives behaviour: different weights → different totals (same inputs).
  4. ``warranty > 0`` for a non-zero bom_line (gap closed — warrantyCost was 0 in Andreas).
  5. ``price`` = supplier unit_price × quantity.
  6. Missing required keys raise ValueError.
  7. Negative values raise ValueError.
  8. ``load_procurement_config`` loads both real JSON files without error.
  9. Weights JSON contains TCO_WEIGHTS + SCORING_WEIGHTS with documented sub-keys.
  10. Tolerances JSON contains MATCH_TOLERANCE + DEFAULT_THRESHOLDS with documented sub-keys.

All tests are plain unit tests (no DB, no Redis, no HTTP).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures — algorithm tests use THESE weights, never Bravo's values
# ---------------------------------------------------------------------------

_FIXTURE_WEIGHTS_A: dict = {
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
_FIXTURE_WEIGHTS_B: dict = {
    "TCO_WEIGHTS": {
        "freight": 0.10,
        "warranty": 0.15,
        "stock": 0.06,
        "delivery_risk": 0.08,
    },
    "SCORING_WEIGHTS": {
        "tco": 0.50,
        "delivery_reliability": 0.20,
        "bid_price": 0.15,
        "tier_bundling": 0.10,
        "kickback_proximity": 0.05,
    },
}

_FIXTURE_TOLERANCES: dict = {
    "MATCH_TOLERANCE": {
        "GREEN_THRESHOLD": 115,
        "YELLOW_THRESHOLD": 70,
    },
    "DEFAULT_THRESHOLDS": {"green": 115, "yellow": 70},
}

_SUPPLIER_100 = {"unit_price": 100.0}
_BOM_LINE_1 = {"quantity": 1}
_BOM_LINE_5 = {"quantity": 5}


# ---------------------------------------------------------------------------
# 1. Result has exactly the 6 documented keys
# ---------------------------------------------------------------------------


def test_result_keys():
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    result = do_calculate_tco(_FIXTURE_WEIGHTS_A, _FIXTURE_TOLERANCES, _SUPPLIER_100, _BOM_LINE_1)
    assert set(result.keys()) == {"price", "freight", "warranty", "stock", "delivery_risk", "total"}


# ---------------------------------------------------------------------------
# 2. total == sum of five components
# ---------------------------------------------------------------------------


def test_total_equals_sum_of_components():
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    result = do_calculate_tco(_FIXTURE_WEIGHTS_A, _FIXTURE_TOLERANCES, _SUPPLIER_100, _BOM_LINE_5)
    expected_total = (
        result["price"]
        + result["freight"]
        + result["warranty"]
        + result["stock"]
        + result["delivery_risk"]
    )
    assert abs(result["total"] - expected_total) < 1e-9, (
        f"total {result['total']} != component sum {expected_total}"
    )


# ---------------------------------------------------------------------------
# 3. Config drives behaviour — different weights → different totals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "weights_a,weights_b",
    [(_FIXTURE_WEIGHTS_A, _FIXTURE_WEIGHTS_B)],
)
def test_different_weights_yield_different_totals(weights_a, weights_b):
    """Same algorithm + same inputs + different weights must yield different totals."""
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    supplier = {"unit_price": 200.0}
    bom_line = {"quantity": 3}

    result_a = do_calculate_tco(weights_a, _FIXTURE_TOLERANCES, supplier, bom_line)
    result_b = do_calculate_tco(weights_b, _FIXTURE_TOLERANCES, supplier, bom_line)

    assert result_a["total"] != result_b["total"], (
        "Different TCO_WEIGHTS must produce different totals — config must drive behaviour"
    )


# ---------------------------------------------------------------------------
# 4. warranty > 0 for a non-zero bom_line (gap closed)
# ---------------------------------------------------------------------------


def test_warranty_greater_than_zero_for_nonzero_bom_line():
    """Closes the warrantyCost=0 gap: warranty must be positive for any non-zero line."""
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    supplier = {"unit_price": 500.0}
    bom_line = {"quantity": 2, "unit_price": 490.0}

    result = do_calculate_tco(_FIXTURE_WEIGHTS_A, _FIXTURE_TOLERANCES, supplier, bom_line)

    assert result["warranty"] > 0, (
        "warranty must be > 0 for a non-zero bom_line — warrantyCost=0 gap is closed"
    )


def test_warranty_is_fraction_of_bom_line_value():
    """Warranty = bom_unit_price × quantity × warranty_weight (formula verification)."""
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    weights = {
        "TCO_WEIGHTS": {
            "freight": 0.05,
            "warranty": 0.10,  # explicit round number for easy assertion
            "stock": 0.03,
            "delivery_risk": 0.04,
        },
        "SCORING_WEIGHTS": {},
    }
    supplier = {"unit_price": 100.0}
    bom_line = {"quantity": 3, "unit_price": 80.0}

    result = do_calculate_tco(weights, _FIXTURE_TOLERANCES, supplier, bom_line)

    # bom_line_value = 80.0 × 3 = 240.0; warranty = 240.0 × 0.10 = 24.0
    expected_warranty = 80.0 * 3 * 0.10
    assert abs(result["warranty"] - expected_warranty) < 1e-9, (
        f"warranty {result['warranty']} != expected {expected_warranty}"
    )


def test_warranty_defaults_to_supplier_price_when_bom_unit_price_absent():
    """When bom_line has no unit_price, warranty uses supplier unit_price (safe default)."""
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    weights = {
        "TCO_WEIGHTS": {
            "freight": 0.05,
            "warranty": 0.10,
            "stock": 0.03,
            "delivery_risk": 0.04,
        },
        "SCORING_WEIGHTS": {},
    }
    supplier = {"unit_price": 100.0}
    bom_line = {"quantity": 2}  # no unit_price key

    result = do_calculate_tco(weights, _FIXTURE_TOLERANCES, supplier, bom_line)

    # bom_line_value defaults to 100.0 × 2 = 200.0; warranty = 200.0 × 0.10 = 20.0
    expected_warranty = 100.0 * 2 * 0.10
    assert abs(result["warranty"] - expected_warranty) < 1e-9
    assert result["warranty"] > 0


# ---------------------------------------------------------------------------
# 5. price = supplier unit_price × quantity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit_price,quantity,expected_price",
    [
        (100.0, 1, 100.0),
        (250.0, 4, 1000.0),
        (0.01, 1000, 10.0),
    ],
)
def test_price_equals_unit_price_times_quantity(unit_price, quantity, expected_price):
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    result = do_calculate_tco(
        _FIXTURE_WEIGHTS_A,
        _FIXTURE_TOLERANCES,
        {"unit_price": unit_price},
        {"quantity": quantity},
    )
    assert abs(result["price"] - expected_price) < 1e-9


# ---------------------------------------------------------------------------
# 6. Missing required keys raise ValueError
# ---------------------------------------------------------------------------


def test_missing_supplier_unit_price_raises():
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    with pytest.raises(ValueError, match="unit_price"):
        do_calculate_tco(_FIXTURE_WEIGHTS_A, _FIXTURE_TOLERANCES, {}, _BOM_LINE_1)


def test_missing_bom_line_quantity_raises():
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    with pytest.raises(ValueError, match="quantity"):
        do_calculate_tco(_FIXTURE_WEIGHTS_A, _FIXTURE_TOLERANCES, _SUPPLIER_100, {})


def test_missing_tco_weights_key_raises():
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    with pytest.raises(KeyError):
        do_calculate_tco({}, _FIXTURE_TOLERANCES, _SUPPLIER_100, _BOM_LINE_1)


# ---------------------------------------------------------------------------
# 7. Negative values raise ValueError
# ---------------------------------------------------------------------------


def test_negative_supplier_unit_price_raises():
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    with pytest.raises(ValueError, match="negative"):
        do_calculate_tco(
            _FIXTURE_WEIGHTS_A,
            _FIXTURE_TOLERANCES,
            {"unit_price": -10.0},
            _BOM_LINE_1,
        )


def test_zero_quantity_raises():
    from nce.vertical_modules.procurement.tco import do_calculate_tco

    with pytest.raises(ValueError, match="positive"):
        do_calculate_tco(
            _FIXTURE_WEIGHTS_A,
            _FIXTURE_TOLERANCES,
            _SUPPLIER_100,
            {"quantity": 0},
        )


# ---------------------------------------------------------------------------
# (b) CONFIG tests — assert real JSON files load and contain documented keys
# ---------------------------------------------------------------------------


def test_load_procurement_config_loads_without_error():
    """Both JSON files must load cleanly via load_procurement_config."""
    from nce.vertical_modules.procurement.tco import load_procurement_config

    weights, tolerances = load_procurement_config()

    assert isinstance(weights, dict), "weights must be a dict"
    assert isinstance(tolerances, dict), "tolerances must be a dict"


def test_weights_json_contains_tco_weights_key():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    weights, _ = load_procurement_config()

    assert "TCO_WEIGHTS" in weights, "weights JSON must contain 'TCO_WEIGHTS'"


def test_weights_json_tco_weights_contains_required_sub_keys():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    weights, _ = load_procurement_config()
    tco_w = weights["TCO_WEIGHTS"]

    for key in ("freight", "warranty", "stock", "delivery_risk"):
        assert key in tco_w, f"TCO_WEIGHTS must contain '{key}'"
        assert isinstance(tco_w[key], (int, float)), f"TCO_WEIGHTS['{key}'] must be numeric"


def test_weights_json_contains_scoring_weights_key():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    weights, _ = load_procurement_config()

    assert "SCORING_WEIGHTS" in weights, "weights JSON must contain 'SCORING_WEIGHTS'"


def test_weights_json_scoring_weights_contains_required_sub_keys():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    weights, _ = load_procurement_config()
    scoring_w = weights["SCORING_WEIGHTS"]

    for key in ("tco", "delivery_reliability", "bid_price", "tier_bundling", "kickback_proximity"):
        assert key in scoring_w, f"SCORING_WEIGHTS must contain '{key}'"


def test_tolerances_json_contains_match_tolerance_key():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    _, tolerances = load_procurement_config()

    assert "MATCH_TOLERANCE" in tolerances, "tolerances JSON must contain 'MATCH_TOLERANCE'"


def test_tolerances_json_match_tolerance_contains_threshold_keys():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    _, tolerances = load_procurement_config()
    mt = tolerances["MATCH_TOLERANCE"]

    assert "GREEN_THRESHOLD" in mt, "MATCH_TOLERANCE must contain 'GREEN_THRESHOLD'"
    assert "YELLOW_THRESHOLD" in mt, "MATCH_TOLERANCE must contain 'YELLOW_THRESHOLD'"
    assert mt["GREEN_THRESHOLD"] > mt["YELLOW_THRESHOLD"], (
        "GREEN_THRESHOLD must be > YELLOW_THRESHOLD"
    )


def test_tolerances_json_contains_default_thresholds_key():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    _, tolerances = load_procurement_config()

    assert "DEFAULT_THRESHOLDS" in tolerances, "tolerances JSON must contain 'DEFAULT_THRESHOLDS'"


def test_tolerances_json_default_thresholds_green_yellow():
    from nce.vertical_modules.procurement.tco import load_procurement_config

    _, tolerances = load_procurement_config()
    dt = tolerances["DEFAULT_THRESHOLDS"]

    assert "green" in dt, "DEFAULT_THRESHOLDS must contain 'green'"
    assert "yellow" in dt, "DEFAULT_THRESHOLDS must contain 'yellow'"


def test_real_config_drives_tco_computation():
    """Real JSON files load and produce a valid TCO result — end-to-end config path."""
    from nce.vertical_modules.procurement.tco import do_calculate_tco, load_procurement_config

    weights, tolerances = load_procurement_config()
    supplier = {"unit_price": 300.0}
    bom_line = {"quantity": 2, "unit_price": 295.0}

    result = do_calculate_tco(weights, tolerances, supplier, bom_line)

    assert set(result.keys()) == {"price", "freight", "warranty", "stock", "delivery_risk", "total"}
    assert result["total"] > 0
    assert result["warranty"] > 0, "Real config must produce warranty > 0"

    component_sum = (
        result["price"]
        + result["freight"]
        + result["warranty"]
        + result["stock"]
        + result["delivery_risk"]
    )
    assert abs(result["total"] - component_sum) < 1e-9
