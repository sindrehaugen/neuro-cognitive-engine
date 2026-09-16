"""
tests/unit/test_business_insights_fabricated_defaults_ratchet.py
================================================================
Ratchet test suite for Wave C-BI3: Eradicate Fabricated Financials & Risk Defaults.

Enforces:
  1. No string, float or int literal may appear as the default argument of a
     .get(_, default) call anywhere under nce/vertical_modules/business_insights/,
     unless explicitly justified in the shrink-only KNOWN_PRESENTATION_DEFAULTS allowlist.
  2. board_pack.py and radar.py have strictly ZERO literal defaults (empty allowlist).
  3. Standing Positive Control: Asserts the AST scanner goes RED when a forbidden
     literal default is introduced.
  4. Shrink-Only Contract: Every entry in the allowlist must match a live call;
     stale entries cause test failure.
  5. Behavioral verification for required period parameter and graceful degradation
     in board_pack.py and radar.py.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from nce.degradation import get_degradation_register
from nce.vertical_modules.business_insights.board_pack import do_generate_board_pack
from nce.vertical_modules.business_insights.radar import do_risk_radar

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BI_DIR = _REPO_ROOT / "nce" / "vertical_modules" / "business_insights"

# ---------------------------------------------------------------------------
# Shrink-only allowlist: (filename, key_name, default_value) -> reason
# ---------------------------------------------------------------------------
# 🔴 This list may only get SHORTER. Adding an entry without an architectural
# justification approved in Charter §13 is forbidden.
KNOWN_PRESENTATION_DEFAULTS: dict[tuple[str, str, Any], str] = {
    ("aggregation.py", "metric_key", 0.0): (
        "aggregation.py:208 -- fallback value 0.0 when aggregating numeric metrics across "
        "dimensional slices where an individual record lacks the metric key."
    ),
    ("ask.py", "actor", "system"): (
        "ask.py:60 -- fallback audit actor identifier when caller principal is not specified."
    ),
    ("brief.py", "principal_role", "executive"): (
        "brief.py:61 -- role gate defaults to 'executive' if unspecified."
    ),
    ("brief.py", "actor", "system"): (
        "brief.py:64 -- fallback audit actor identifier for morning brief generation."
    ),
    ("kpi.py", "period", "live"): (
        "kpi.py:69 -- defaults query period to 'live' snapshot evaluation if unspecified."
    ),
    ("kpi.py", "engine", "unknown"): (
        "kpi.py:80 -- fallback domain label if a KPI definition dict lacks an 'engine' property."
    ),
    ("kpi.py", "unit", ""): (
        "kpi.py:121 -- formatting unit string defaults to empty string for unitless metrics."
    ),
    ("scenario.py", "principal_role", "executive"): (
        "scenario.py:72 -- role gate defaults to 'executive' if unspecified."
    ),
    ("scenario.py", "actor", "system"): (
        "scenario.py:75 -- fallback audit actor identifier for scenario simulation runs."
    ),
    ("scenario.py", "name", "Forward Commercial & Capital Scenario"): (
        "scenario.py:76 -- default human-readable label for simulation graph nodes."
    ),
    ("scenario.py", "months", 6): (
        "scenario.py:98 -- simulation forward horizon in months if unspecified in assumptions."
    ),
    ("scenario.py", "baseline_cash", 2000000.0): (
        "scenario.py:99 -- default starting cash assumption for forward Monte Carlo cashflow model."
    ),
    ("scenario.py", "monthly_burn", 150000.0): (
        "scenario.py:100 -- default monthly operational burn assumption if unspecified."
    ),
    ("scenario.py", "available_capacity_fte", 10.0): (
        "scenario.py:131 -- baseline staffing capacity assumption for forward pipeline feasibility."
    ),
    ("scenario.py", "value", 0.0): (
        "scenario.py:105, 107, 157 -- default deal value 0.0 when evaluating pipeline assumptions."
    ),
    ("scenario.py", "staff_needed_fte", 0.0): (
        "scenario.py:109 -- default staffing requirement 0.0 when deal assumptions omit FTE requirements."
    ),
    ("scenario.py", "win_probability", 0.5): (
        "scenario.py:107, 156 -- default expected booking win probability 0.5 when unspecified."
    ),
}


def _scan_file_for_literal_defaults(file_path: Path) -> list[tuple[int, str, Any]]:
    """Return list of (line_no, key_repr, literal_value) for .get() calls with literal defaults."""
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(file_path))

    results: list[tuple[int, str, Any]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
        ):
            if len(node.args) >= 2:
                default_arg = node.args[1]
                # Check if literal string, int, or float (excluding pure bool)
                if (
                    isinstance(default_arg, ast.Constant)
                    and isinstance(default_arg.value, (str, int, float))
                    and not isinstance(default_arg.value, bool)
                ):
                    key_arg = node.args[0]
                    if isinstance(key_arg, ast.Constant) and isinstance(key_arg.value, str):
                        key_name = key_arg.value
                    elif isinstance(key_arg, ast.Name):
                        key_name = key_arg.id
                    else:
                        key_name = ast.dump(key_arg)
                    results.append((node.lineno, key_name, default_arg.value))
    return results


def test_board_pack_and_radar_have_strictly_zero_literal_defaults() -> None:
    """Wave C-BI3 requirement: board_pack.py and radar.py must have ZERO literal defaults."""
    for filename in ["board_pack.py", "radar.py"]:
        path = _BI_DIR / filename
        assert path.exists(), f"Expected file {path} to exist"
        violations = _scan_file_for_literal_defaults(path)
        assert len(violations) == 0, (
            f"{filename} contains {len(violations)} forbidden literal defaults: {violations}"
        )


def test_business_insights_has_zero_unlisted_literal_defaults() -> None:
    """Scan all python files in business_insights for unapproved .get(_, literal) calls."""
    unapproved: list[str] = []

    for file_path in sorted(_BI_DIR.glob("*.py")):
        filename = file_path.name
        violations = _scan_file_for_literal_defaults(file_path)

        for line_no, key_name, val in violations:
            lookup_key = (filename, key_name, val)
            if lookup_key not in KNOWN_PRESENTATION_DEFAULTS:
                unapproved.append(
                    f"{filename}:{line_no} -> .get({key_name!r}, {val!r}) is not in KNOWN_PRESENTATION_DEFAULTS"
                )

    assert len(unapproved) == 0, (
        f"Found {len(unapproved)} unapproved literal defaults under business_insights/:\n"
        + "\n".join(unapproved)
    )


def test_allowlist_is_shrink_only_and_has_no_stale_entries() -> None:
    """Assert every entry in KNOWN_PRESENTATION_DEFAULTS matches a live call (shrink-only contract)."""
    observed_entries: set[tuple[str, str, Any]] = set()

    for file_path in _BI_DIR.glob("*.py"):
        filename = file_path.name
        violations = _scan_file_for_literal_defaults(file_path)
        for _, key_name, val in violations:
            observed_entries.add((filename, key_name, val))

    stale_entries = set(KNOWN_PRESENTATION_DEFAULTS.keys()) - observed_entries
    assert len(stale_entries) == 0, (
        f"KNOWN_PRESENTATION_DEFAULTS contains {len(stale_entries)} stale entries that no longer match live code:\n"
        + "\n".join(str(e) for e in stale_entries)
    )


def test_positive_control_catches_forbidden_literal_default(tmp_path: Path) -> None:
    """Positive Control: Proves the scanner detects synthetic code with forbidden defaults and goes RED."""
    bad_code = (
        "def fake_func(data):\n"
        '    rev = data.get("revenue", "$4,850,000")\n'
        '    growth = data.get("pipeline_growth_pct", 30.0)\n'
        '    rate = data.get("breach_rate", 18)\n'
        "    return rev, growth, rate\n"
    )
    test_file = tmp_path / "synthetic_defect.py"
    test_file.write_text(bad_code, encoding="utf-8")

    violations = _scan_file_for_literal_defaults(test_file)
    assert len(violations) == 3
    assert (2, "revenue", "$4,850,000") in violations
    assert (3, "pipeline_growth_pct", 30.0) in violations
    assert (4, "breach_rate", 18) in violations


# ---------------------------------------------------------------------------
# Behavioral tests for C-BI3 logic
# ---------------------------------------------------------------------------


class DummyEngine:
    """Mock engine with no active pool."""

    def __init__(self) -> None:
        self.pg_pool = None
        self.pool = None


@pytest.mark.asyncio
async def test_board_pack_requires_period_parameter() -> None:
    """do_generate_board_pack must raise ValueError if period and quarter are missing or blank."""
    engine = DummyEngine()
    ns_id = str(uuid4())

    # Missing period & quarter
    with pytest.raises(ValueError) as exc:
        await do_generate_board_pack(engine, {"namespace_id": ns_id})
    assert "period is required for do_generate_board_pack" in str(exc.value)

    # Empty string period
    with pytest.raises(ValueError) as exc:
        await do_generate_board_pack(engine, {"namespace_id": ns_id, "period": "   "})
    assert "period is required for do_generate_board_pack" in str(exc.value)


@pytest.mark.asyncio
async def test_board_pack_grace_degrades_when_engine_data_unavailable() -> None:
    """When engine data is omitted, board pack must grace-degrade and record degradation events."""
    engine = DummyEngine()
    ns_id = str(uuid4())

    register = get_degradation_register()
    register.clear(ns_id)

    res = await do_generate_board_pack(
        engine,
        {
            "namespace_id": ns_id,
            "period": "2026-Q3",
            # No data_override provided: economy, sales, resources omitted
        },
    )

    assert res["status"] == "ok"
    assert res["period"] == "2026-Q3"
    bp = res["board_pack"]
    sections = bp["sections"]

    # Economy financial pulse grace-degraded
    fin = sections["financial_pulse"]
    assert fin["degraded"] is True
    assert fin["status"] == "not available yet"
    assert fin["revenue"] == "not available yet"
    assert fin["gross_margin_pct"] is None
    assert fin["arr"] == "not available yet"
    assert fin["cash_runway_months"] is None

    # Sales pipeline grace-degraded
    sales = sections["sales_pipeline"]
    assert sales["degraded"] is True
    assert sales["status"] == "not available yet"
    assert sales["pipeline_total_value"] == "not available yet"
    assert sales["pipeline_growth_pct"] is None
    assert sales["top_deals"] == []

    # Resources operational capacity grace-degraded
    cap = sections["operational_capacity"]
    assert cap["degraded"] is True
    assert cap["status"] == "not available yet"
    assert cap["display_value"] == "not available yet"
    assert cap["team_utilization_pct"] is None

    # Verify degradation records in register
    degradations = register.get_degradations(ns_id)
    codes = {d["code"] for d in degradations}
    assert "board_pack_economy_unavailable" in codes
    assert "board_pack_sales_unavailable" in codes
    assert "board_pack_resources_unavailable" in codes


@pytest.mark.asyncio
async def test_risk_radar_marks_missing_data_as_unevaluated_not_clean() -> None:
    """A risk rule with no input is not 'no risk'; radar must report it as unevaluated distinctly."""
    engine = DummyEngine()
    ns_id = str(uuid4())

    register = get_degradation_register()
    register.clear(ns_id)

    # Empty data_override: no sales, economy, inventory, support, or agreements data
    res = await do_risk_radar(engine, {"namespace_id": ns_id, "data_override": {}})

    assert res["status"] == "ok"
    assert res["findings"] == []  # Zero fabricated findings!

    unevaluated = res["unevaluated_rules"]
    assert len(unevaluated) == 3
    uneval_ids = {r["rule_id"] for r in unevaluated}
    assert "pipeline_up_capacity_redlined" in uneval_ids
    assert "margin_erosion_dead_stock" in uneval_ids
    assert "sla_breach_trend_renewal_due" in uneval_ids

    for r in unevaluated:
        assert r["status"] == "unevaluated"
        assert r["reason"] == "data_unavailable"
        assert "unavailable" in r["detail"].lower()

    # Evaluated clear rules list is empty because none had data to evaluate
    assert res["evaluated_clear_rules"] == []

    # Verify degradation register tracking
    degradations = register.get_degradations(ns_id)
    codes = {d["code"] for d in degradations}
    assert "radar_pipeline_up_capacity_redlined_sales_unavailable" in codes
    assert "radar_margin_erosion_dead_stock_data_unavailable" in codes
    assert "radar_sla_breach_trend_renewal_due_data_unavailable" in codes


@pytest.mark.asyncio
async def test_risk_radar_evaluates_clean_when_below_thresholds() -> None:
    """When real data is supplied and metrics are below thresholds, rules report evaluated_clear."""
    engine = DummyEngine()
    ns_id = str(uuid4())

    params = {
        "namespace_id": ns_id,
        "data_override": {
            "sales": {
                "pipeline_growth_pct": 12.0,  # Below 25.0 threshold
                "pipeline_value": "$500,000",
                "provenance_nodes": ["quote:QUOTE-LOW"],
            },
            "economy": {
                "margin_compression_bps": 50,  # Below 200 bps threshold
                "reconciled": True,
                "provenance_nodes": ["invoice:INV-LOW"],
            },
            "inventory": {
                "dead_stock_value": 15000.0,  # Below 50000.0 threshold
                "provenance_nodes": ["sku:SKU-LOW"],
            },
            "support": {
                "sla_breach_rate_pct": 5.0,  # Below 15.0 threshold
                "provenance_nodes": ["ticket:TICK-LOW"],
            },
            "agreements": {
                "renewal_window_days": 180,  # Above 90 days window
                "provenance_nodes": ["agreement:AGR-LOW"],
            },
        },
    }

    res = await do_risk_radar(engine, params)
    assert res["status"] == "ok"
    assert res["findings"] == []
    assert res["unevaluated_rules"] == []

    clear_rules = res["evaluated_clear_rules"]
    assert len(clear_rules) == 3
    for r in clear_rules:
        assert r["status"] == "evaluated_clear"
