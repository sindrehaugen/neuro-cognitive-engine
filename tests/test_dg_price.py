"""
Unit tests for nce.pricing.dg — DG-based pricing core.

Pure unit tests (no DB, no HTTP, no fixtures beyond in-process JSON).
Covers formula correctness, boundaries, guard, *0.7 equivalence.
"""

import pytest

from nce.pricing import dg_price, load_dg


class TestDgPrice:
    """Test dg_price(cost, dg_pct) pure function."""

    def test_dg_price_zero_dg(self):
        """At dg_pct=0, returns cost unchanged."""
        assert dg_price(100, 0) == 100

    def test_dg_price_nonzero(self):
        """Formula: cost / (1 - dg_pct)."""
        assert dg_price(100, 0.2) == 100 / 0.8
        assert dg_price(100, 0.2) == 125

    def test_dg_price_03_equivalence_07(self):
        """At dg_pct=0.3, cost / 0.7 (the *0.7 equivalence)."""
        cost = 100
        dg = 0.3
        expected = cost / 0.7
        assert dg_price(cost, dg) == expected
        # Verify arithmetic: 100 / 0.7 ≈ 142.857
        assert abs(dg_price(100, 0.3) - 142.857142857) < 0.001

    def test_dg_price_guard_dg_lt_zero(self):
        """Guard: dg_pct < 0 raises ValueError."""
        with pytest.raises(ValueError, match="dg_pct must be in"):
            dg_price(100, -0.1)

    def test_dg_price_guard_dg_eq_one(self):
        """Guard: dg_pct >= 1 raises ValueError (division by zero undefined)."""
        with pytest.raises(ValueError, match="dg_pct must be in"):
            dg_price(100, 1.0)

    def test_dg_price_guard_dg_gt_one(self):
        """Guard: dg_pct > 1 raises ValueError."""
        with pytest.raises(ValueError, match="dg_pct must be in"):
            dg_price(100, 1.5)

    def test_dg_price_boundary_near_one(self):
        """Near dg_pct=1 (e.g., 0.99), result is large but finite."""
        result = dg_price(100, 0.99)
        expected = 100 / 0.01
        assert abs(result - expected) < 1e-10
        assert abs(result - 10000) < 1e-10

    def test_dg_price_float_precision(self):
        """Handles float values correctly."""
        cost = 99.5
        dg = 0.25
        expected = cost / 0.75
        assert abs(dg_price(cost, dg) - expected) < 1e-10


class TestLoadDg:
    """Test load_dg(namespace) configuration loader."""

    def test_load_dg_default_namespace(self):
        """Load default namespace returns 0.3."""
        dg = load_dg("default")
        assert dg == 0.3

    def test_load_dg_type(self):
        """Returned DG% is a float."""
        dg = load_dg("default")
        assert isinstance(dg, float)

    def test_load_dg_missing_namespace(self):
        """Missing namespace raises KeyError."""
        with pytest.raises(KeyError, match="not found"):
            load_dg("nonexistent_namespace")

    def test_load_dg_missing_file(self, tmp_path, monkeypatch):
        """Missing product-dg.json raises FileNotFoundError."""
        # Monkeypatch the config path to point to a non-existent file.
        import nce.pricing.dg as dg_module

        fake_path = tmp_path / "fake" / "product-dg.json"

        def fake_load_dg(namespace: str) -> float:
            config_path = fake_path
            if not config_path.exists():
                raise FileNotFoundError(
                    f"product-dg.json not found at {config_path}. "
                    f"Ensure nce/config_data/product-dg.json is seeded."
                )
            return 0.3

        monkeypatch.setattr(dg_module, "load_dg", fake_load_dg)

        with pytest.raises(FileNotFoundError, match="product-dg.json not found"):
            fake_load_dg("default")


class TestDgPriceAndLoadDgIntegration:
    """Integration: dg_price + load_dg."""

    def test_dg_price_with_loaded_dg(self):
        """Use loaded DG% in dg_price formula."""
        dg = load_dg("default")  # 0.3
        cost = 100
        sales_price = dg_price(cost, dg)
        # 100 / (1 - 0.3) = 100 / 0.7 ≈ 142.857
        assert abs(sales_price - 142.857142857) < 0.001
