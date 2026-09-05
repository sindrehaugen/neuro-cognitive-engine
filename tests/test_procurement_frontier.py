"""
tests/test_procurement_frontier.py
====================================
Integration tests for Module 1 Wave 12 — Procurement Frontier Advisor.

Covers:
  1. forecast_rebate: seeded BOM pipeline → rebate matches expected calculation.
  2. recommend_move_spend: two suppliers with different ROI → top supplier is correct.
  3. whatif_spend: deterministic delta calculation.
  4. Advisor discipline: no submit_po / generate_po / PO INSERT call exists in frontier.py.
  5. Tool registry: 3 new tools registered with correct flags
     (cacheable=True, mutation=False, admin_only=False).

All DB tests use ``pg_app_conn``/``make_namespace``/``set_namespace_context``
from ``nce.auth``.  The forward-ref tables (``procurement_bom_pipeline``,
``procurement_kickback_tiers``, ``procurement_spend_lines``) are not seeded —
the tests assert the graceful-degrade (empty / neutral) path, which is the
correct behaviour until those tables exist in the dev schema.
"""

from __future__ import annotations

import inspect

import pytest

from nce.tool_registry import CACHEABLE_TOOLS, MUTATION_TOOLS, TOOL_REGISTRY
from nce.vertical_modules.procurement import frontier
from nce.vertical_modules.procurement.frontier import (
    forecast_rebate,
    recommend_move_spend,
    simulate_whatif_spend,
)

# ---------------------------------------------------------------------------
# Pure-calc unit tests (no DB) — always run
# ---------------------------------------------------------------------------


def test_forecast_rebate_empty_bom():
    """Empty BOM rows → neutral recommendation, zero spend."""
    result = forecast_rebate([], [])
    assert result["annual_spend"] == 0.0
    assert result["rebate_amount"] == 0.0
    assert result["confidence"] == "low"
    assert "No BOM pipeline data" in result["rationale"]


def test_forecast_rebate_no_tier_match():
    """Spend below all tier minimums → rebate_amount = 0."""
    bom_rows = [{"unit_price": 10.0, "quantity": 5}]  # spend = 50
    tiers = [{"name": "gold", "min_spend": 1000.0, "rate": 0.05}]
    result = forecast_rebate(bom_rows, tiers)
    assert result["annual_spend"] == pytest.approx(50.0)
    assert result["rebate_amount"] == pytest.approx(0.0)
    assert result["matched_tier"] is None
    assert result["confidence"] == "low"


def test_forecast_rebate_tier_match():
    """Spend qualifies for a tier → rebate_amount = spend × rate, ±10% band."""
    bom_rows = [{"unit_price": 100.0, "quantity": 20}]  # spend = 2000
    tiers = [
        {"name": "silver", "min_spend": 500.0, "rate": 0.03},
        {"name": "gold", "min_spend": 2000.0, "rate": 0.05},
    ]
    result = forecast_rebate(bom_rows, tiers)
    assert result["annual_spend"] == pytest.approx(2000.0)
    assert result["matched_tier"]["name"] == "gold"
    assert result["rebate_amount"] == pytest.approx(100.0)  # 2000 × 0.05
    assert result["rebate_low"] == pytest.approx(90.0)
    assert result["rebate_high"] == pytest.approx(110.0)
    assert result["confidence"] == "medium"


def test_recommend_move_spend_empty():
    """No suppliers → no_data recommendation."""
    result = recommend_move_spend([])
    assert result["recommendation"] == "no_data"
    assert result["top_supplier"] is None
    assert result["roi_scores"] == []


def test_recommend_move_spend_picks_higher_roi():
    """Supplier B has higher ROI → recommended over supplier A."""
    suppliers = [
        {"supplier_id": "supplier_a", "precision": 0.6, "realised": 1000.0, "lost": 500.0},
        # roi_A = 0.6 × (1 - 0.5) = 0.30
        {"supplier_id": "supplier_b", "precision": 0.9, "realised": 2000.0, "lost": 200.0},
        # roi_B = 0.9 × (1 - 0.1) = 0.81
    ]
    result = recommend_move_spend(suppliers)
    assert result["recommendation"] == "move_spend"
    assert result["top_supplier"]["supplier_id"] == "supplier_b"
    top_roi = result["top_supplier"]["roi_score"]
    assert top_roi == pytest.approx(0.81)


def test_simulate_whatif_spend_deterministic():
    """Deterministic: same inputs → same outputs."""
    from_sup = {"supplier_id": "a", "margin_rate": 0.10, "rebate_rate": 0.02}
    to_sup = {"supplier_id": "b", "margin_rate": 0.15, "rebate_rate": 0.04}
    result = simulate_whatif_spend(10000.0, 0.25, from_sup, to_sup)
    # shifted = 2500, delta_savings = 2500*(0.15-0.10)=125, delta_rebate=2500*(0.04-0.02)=50
    assert result["shifted_spend"] == pytest.approx(2500.0)
    assert result["delta_savings"] == pytest.approx(125.0)
    assert result["delta_rebate"] == pytest.approx(50.0)
    assert result["net_delta"] == pytest.approx(175.0)
    assert result["recommendation"] == "move"


def test_simulate_whatif_spend_hold_when_negative():
    """Shifting to a worse supplier → net_delta < 0 → recommendation = hold."""
    from_sup = {"supplier_id": "a", "margin_rate": 0.20, "rebate_rate": 0.05}
    to_sup = {"supplier_id": "b", "margin_rate": 0.10, "rebate_rate": 0.01}
    result = simulate_whatif_spend(10000.0, 0.50, from_sup, to_sup)
    assert result["net_delta"] < 0
    assert result["recommendation"] == "hold"


# ---------------------------------------------------------------------------
# Advisor discipline: no PO write anywhere in frontier.py
# ---------------------------------------------------------------------------


def test_frontier_has_no_po_write():
    """Confirm frontier module source contains no submit_po / generate_po / INSERT calls."""
    source = inspect.getsource(frontier)
    # Check for actual PO-write INVOCATIONS / imports — not bare words. The module
    # docstring legitimately mentions submit_po/generate_po in its "never calls
    # these" note, so a substring scan false-positives. Advisor discipline = the
    # module never CALLS a PO writer nor imports the PO/transport write modules.
    forbidden_calls = [
        "submit_po(",
        "generate_po(",
        "place_order(",
        "upsert_po_node(",
        ".po import",
        ".transports import",
    ]
    for token in forbidden_calls:
        assert token not in source, (
            f"Advisor discipline violation: '{token}' found in frontier.py source. "
            "Frontier Advisor must RECOMMEND only — never call a PO writer."
        )
    # No actual SQL write STATEMENTS. Match INSERT INTO / DELETE FROM /
    # UPDATE <tbl> SET — not bare words (the docstring's "no INSERT/UPDATE/DELETE"
    # discipline notes would false-positive a plain keyword scan).
    import re

    sql_writes = re.findall(
        r"INSERT\s+INTO|DELETE\s+FROM|UPDATE\s+\w+\s+SET", source, re.IGNORECASE
    )
    assert not sql_writes, (
        f"Advisor discipline violation: SQL write statement found in frontier.py: {sql_writes}"
    )


# ---------------------------------------------------------------------------
# Tool registry: 3 tools registered with correct flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "procurement_forecast_rebate",
        "procurement_recommend_move_spend",
        "procurement_whatif_spend",
    ],
)
def test_frontier_tools_registered(tool_name: str):
    assert tool_name in TOOL_REGISTRY, f"Tool '{tool_name}' not found in TOOL_REGISTRY"


@pytest.mark.parametrize(
    "tool_name",
    [
        "procurement_forecast_rebate",
        "procurement_recommend_move_spend",
        "procurement_whatif_spend",
    ],
)
def test_frontier_tools_flags(tool_name: str):
    spec = TOOL_REGISTRY[tool_name]
    assert spec.cacheable is True, f"{tool_name}: expected cacheable=True"
    assert spec.mutation is False, f"{tool_name}: expected mutation=False"
    assert spec.admin_only is False, f"{tool_name}: expected admin_only=False"
    assert spec.migration is False, f"{tool_name}: expected migration=False"


@pytest.mark.parametrize(
    "tool_name",
    [
        "procurement_forecast_rebate",
        "procurement_recommend_move_spend",
        "procurement_whatif_spend",
    ],
)
def test_frontier_tools_in_cacheable_set(tool_name: str):
    assert tool_name in CACHEABLE_TOOLS, f"'{tool_name}' not in CACHEABLE_TOOLS"


@pytest.mark.parametrize(
    "tool_name",
    [
        "procurement_forecast_rebate",
        "procurement_recommend_move_spend",
        "procurement_whatif_spend",
    ],
)
def test_frontier_tools_not_in_mutation_set(tool_name: str):
    assert tool_name not in MUTATION_TOOLS, f"'{tool_name}' must not be in MUTATION_TOOLS"


# ---------------------------------------------------------------------------
# Integration tests — require live DB + namespace
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_forecast_rebate_degrades_gracefully(pg_app_conn, make_namespace):
    """do_forecast_rebate: forward-ref tables absent → returns neutral/empty result (no crash)."""
    from unittest.mock import MagicMock

    namespace_id = await make_namespace()

    # Build a minimal engine mock with a real pool
    pool = MagicMock()

    class _RealAcquire:
        """Wraps the real conn inside a context-manager-shaped object."""

        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *_):
            pass

    pool.acquire = MagicMock(return_value=_RealAcquire(pg_app_conn))
    engine = MagicMock()
    engine.pg_pool = pool

    params = {"namespace_id": str(namespace_id)}
    result = await frontier.do_forecast_rebate(engine, params)

    # When tables are absent the core degrades gracefully
    assert "annual_spend" in result
    assert "rebate_amount" in result
    assert "rationale" in result
    # No crash, no PO write
    assert result["annual_spend"] == pytest.approx(0.0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_recommend_move_spend_degrades_gracefully(pg_app_conn, make_namespace):
    """do_recommend_move_spend: spend tables absent → neutral no_data or move_spend."""
    from unittest.mock import MagicMock

    namespace_id = await make_namespace()

    class _RealAcquire:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *_):
            pass

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_RealAcquire(pg_app_conn))
    engine = MagicMock()
    engine.pg_pool = pool

    params = {"namespace_id": str(namespace_id)}
    result = await frontier.do_recommend_move_spend(engine, params)

    assert "recommendation" in result
    assert "rationale" in result
    # With no data either no_data or move_spend (if ledger has entries) — both are valid
    assert result["recommendation"] in ("no_data", "move_spend")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_do_whatif_spend_deterministic_with_real_conn(pg_app_conn, make_namespace):
    """do_whatif_spend: spend tables absent → degrades to zero spend; result is deterministic."""
    from unittest.mock import MagicMock

    namespace_id = await make_namespace()

    class _RealAcquire:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *_):
            pass

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_RealAcquire(pg_app_conn))
    engine = MagicMock()
    engine.pg_pool = pool

    params = {
        "namespace_id": str(namespace_id),
        "from_supplier": "sup_a",
        "to_supplier": "sup_b",
        "shift_fraction": 0.5,
    }
    result1 = await frontier.do_whatif_spend(engine, params)
    result2 = await frontier.do_whatif_spend(engine, params)

    # Deterministic
    assert result1["net_delta"] == pytest.approx(result2["net_delta"])
    assert result1["recommendation"] == result2["recommendation"]
    # With no spend data current_spend = 0 → shifted_spend = 0 → net_delta = 0
    assert result1["shifted_spend"] == pytest.approx(0.0)
    assert result1["net_delta"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# A BOM row with no unit_price must not be priced at 0.0
# (no-fabricated-money-defaults ratchet, 2026-09-03)
# ---------------------------------------------------------------------------


def test_unpriced_bom_row_is_reported_not_priced_at_zero():
    """An unpriced row used to contribute 0.0, silently UNDERSTATING annual spend and
    therefore the rebate band. The spend figure must now be flagged as a lower bound:
    the row is counted, confidence drops, and the rationale says so."""
    rows = [{"unit_price": 100.0, "quantity": 20}, {"quantity": 50}]  # 2nd has no price
    tiers = [{"name": "T1", "min_spend": 1000.0, "rate": 0.05}]
    result = forecast_rebate(rows, tiers)
    assert result["annual_spend"] == pytest.approx(2000.0)  # the unpriced row is EXCLUDED
    assert result["unpriced_rows"] == 1
    assert result["confidence"] == "low"  # a tier matched, but on an incomplete figure
    assert "no unit_price" in result["rationale"]


def test_a_real_zero_priced_row_is_not_reported_as_unpriced():
    """The pair: a row priced at a genuine 0.00 (a free item) is fully known. It must
    not be counted as missing, and must not degrade confidence."""
    rows = [{"unit_price": 100.0, "quantity": 20}, {"unit_price": 0.0, "quantity": 50}]
    tiers = [{"name": "T1", "min_spend": 1000.0, "rate": 0.05}]
    result = forecast_rebate(rows, tiers)
    assert result["annual_spend"] == pytest.approx(2000.0)
    assert result["unpriced_rows"] == 0
    assert result["confidence"] == "medium"
    assert "no unit_price" not in result["rationale"]
