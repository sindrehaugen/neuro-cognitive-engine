"""Unit tests for the signed-quote money guard in sales/signing.py.

Regression cover for the fabricated-baseline defect: do_on_signed_callback used to
fall back to a hardcoded 0.3 margin / 1000.0 total via `or`-chains when the signed
quote carried no price data, freezing that invented figure as an immutable baseline.
Money must fail closed instead (§9.3).
"""

from __future__ import annotations

import math

import pytest

from nce.vertical_modules.sales.signing import (
    MissingSignedAmountError,
    _require_money_field,
)

MARGIN_KEYS = ("margin", "signed_margin_pct")
TOTAL_KEYS = ("total_price", "signed_total_nok", "unit_price")


def _margin(quote: dict) -> float:
    return _require_money_field(
        quote, MARGIN_KEYS, label="signed_margin_pct", quote_id="Q1", minimum=0.0, maximum=1.0
    )


def _total(quote: dict) -> float:
    return _require_money_field(
        quote, TOTAL_KEYS, label="signed_total_nok", quote_id="Q1", minimum=0.0
    )


class TestNoFabrication:
    """The core defect: absent price data must never become an invented number."""

    def test_missing_margin_raises_instead_of_defaulting_to_0_3(self):
        with pytest.raises(MissingSignedAmountError) as exc:
            _margin({"total_price": 250000.0})
        assert "missing" in str(exc.value)

    def test_missing_total_raises_instead_of_defaulting_to_1000(self):
        with pytest.raises(MissingSignedAmountError) as exc:
            _total({"margin": 0.35})
        assert "missing" in str(exc.value)

    def test_empty_quote_raises(self):
        with pytest.raises(MissingSignedAmountError):
            _margin({})
        with pytest.raises(MissingSignedAmountError):
            _total({})

    def test_error_names_the_quote_and_the_accepted_keys(self):
        with pytest.raises(MissingSignedAmountError) as exc:
            _total({})
        msg = str(exc.value)
        assert "Q1" in msg
        assert "total_price" in msg


class TestZeroIsPreserved:
    """`or`-chains silently replaced a legitimate 0; presence must be `is not None`."""

    def test_zero_margin_is_kept_not_replaced(self):
        assert _margin({"margin": 0.0}) == 0.0

    def test_zero_total_is_kept_not_replaced(self):
        assert _total({"total_price": 0.0}) == 0.0

    def test_zero_does_not_fall_through_to_later_key(self):
        # Old behaviour: 0.0 is falsy -> silently used signed_total_nok (99999.0).
        assert _total({"total_price": 0.0, "signed_total_nok": 99999.0}) == 0.0


class TestMalformedValuesFailClosed:
    def test_nan_rejected(self):
        with pytest.raises(MissingSignedAmountError, match="not finite"):
            _total({"total_price": float("nan")})

    def test_infinity_rejected(self):
        with pytest.raises(MissingSignedAmountError, match="not finite"):
            _total({"total_price": float("inf")})

    def test_nan_string_rejected(self):
        with pytest.raises(MissingSignedAmountError, match="not finite"):
            _total({"total_price": "nan"})

    def test_non_numeric_string_rejected(self):
        with pytest.raises(MissingSignedAmountError, match="not numeric"):
            _total({"total_price": "1 000 NOK"})

    def test_bool_rejected(self):
        # bool is an int subclass: float(True) == 1.0 would silently become money.
        with pytest.raises(MissingSignedAmountError, match="boolean"):
            _total({"total_price": True})

    def test_negative_total_rejected(self):
        with pytest.raises(MissingSignedAmountError, match="out of range"):
            _total({"total_price": -5.0})

    def test_margin_above_one_rejected(self):
        # Signature 2 of do_freeze_baseline does NOT range-check, unlike signature 1.
        with pytest.raises(MissingSignedAmountError, match="out of range"):
            _margin({"margin": 1.5})

    def test_negative_margin_rejected(self):
        with pytest.raises(MissingSignedAmountError, match="out of range"):
            _margin({"margin": -0.1})


class TestHappyPath:
    def test_real_values_pass_through(self):
        quote = {"margin": 0.35, "total_price": 250000.0}
        assert _margin(quote) == 0.35
        assert _total(quote) == 250000.0

    def test_numeric_strings_are_accepted(self):
        assert _total({"total_price": "250000.0"}) == 250000.0

    def test_first_present_key_wins(self):
        assert _margin({"margin": 0.4, "signed_margin_pct": 0.9}) == 0.4

    def test_falls_through_to_later_key_when_earlier_absent(self):
        assert _total({"unit_price": 1500.0}) == 1500.0
        assert _margin({"signed_margin_pct": 0.22}) == 0.22

    def test_boundary_values_allowed(self):
        assert _margin({"margin": 1.0}) == 1.0
        assert _margin({"margin": 0.0}) == 0.0
        assert math.isclose(_total({"total_price": 0.01}), 0.01)
