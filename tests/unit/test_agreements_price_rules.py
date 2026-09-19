"""tests.unit.test_agreements_price_rules — Unit tests for Wave B-10.

Validates Agreements-owned Config-as-IP index series and price rules:
  1. Config-as-IP loading & schema integrity.
  2. Index series queries, filtering, and multi-period uplift calculations.
  3. Pricing rules queries, filtering, and multi-type rule evaluation.
  4. MCP tool handlers and namespace opt-in gates.
  5. Economy validate contract integration (resolving uplifts via index series / price rules).
  6. Contract lookup across both economy_contracts and agreements tables.
  7. REST API route handling.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce import admin_state
from nce.admin_handlers import agreements as agreements_admin_handlers
from nce.mcp_errors import McpError
from nce.vertical_modules.agreements.index_series import (
    calculate_index_uplift,
    do_calculate_index_adjustment,
    get_index_series,
    load_index_series,
)
from nce.vertical_modules.agreements.mcp_handlers import (
    handle_agreements_calculate_index_adjustment,
    handle_agreements_evaluate_price_rule,
    handle_agreements_get_index_series,
    handle_agreements_get_price_rules,
)
from nce.vertical_modules.agreements.price_rules import (
    evaluate_price_rule,
    get_price_rules,
    load_price_rules,
)
from nce.vertical_modules.economy.contracts import do_validate_contract

_NS_ID = "00000000-0000-4000-8000-000000000099"
_CTR_ID = "CTR-TEST-2026"


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_mock_engine(
    *,
    agreements_enabled: bool = True,
    economy_enabled: bool = True,
    economy_contract_row: dict[str, Any] | None = None,
    agreement_row: dict[str, Any] | None = None,
) -> MagicMock:
    conn = AsyncMock()

    async def _fetchrow(query: str, *args: Any) -> Any:
        q_norm = " ".join(query.split()).lower()
        if "from namespaces" in q_norm:
            return {
                "agreements_enabled": agreements_enabled,
                "economy_enabled": economy_enabled,
            }
        if "from economy_contracts" in q_norm:
            return economy_contract_row
        if "from agreements" in q_norm:
            return agreement_row
        return None

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.transaction = MagicMock(return_value=_AsyncCtx(conn))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


# ---------------------------------------------------------------------------
# 1. Config-as-IP Loading & Integrity Tests
# ---------------------------------------------------------------------------


def test_load_index_series_integrity() -> None:
    series = load_index_series()
    assert len(series) >= 5
    ids = {s["series_id"] for s in series}
    assert "SSB_KPI" in ids
    assert "SSB_KPI_JAE" in ids
    assert "SSB_IKT_TJENESTER" in ids
    assert "SSB_LONN_IKT" in ids
    assert "EU_HICP" in ids

    # Validate SSB_KPI measurements
    kpi = next(s for s in series if s["series_id"] == "SSB_KPI")
    assert "measurements" in kpi
    assert "2023" in kpi["measurements"]
    assert "2024" in kpi["measurements"]
    assert kpi["measurements"]["2024"] > kpi["measurements"]["2023"]


def test_load_price_rules_integrity() -> None:
    rules = load_price_rules()
    assert len(rules) >= 6
    ids = {r["rule_id"] for r in rules}
    assert "RULE_KPI_STANDARD" in ids
    assert "RULE_KPI_80_PCT" in ids
    assert "RULE_SLA_ROOM_CATEGORY" in ids
    assert "RULE_VOLUME_ROOMS" in ids
    assert "RULE_COMMITMENT_3YR" in ids
    assert "RULE_MINIMUM_FEE_FLOOR" in ids


# ---------------------------------------------------------------------------
# 2. Index Series Domain & Uplift Calculation Tests
# ---------------------------------------------------------------------------


def test_get_index_series_filters() -> None:
    # Filter by series_id
    res = get_index_series(series_id="SSB_KPI")
    assert len(res) == 1
    assert res[0]["code"] == "KPI"

    # Search filter
    res_search = get_index_series(search="Core inflation")
    assert len(res_search) >= 1
    assert res_search[0]["series_id"] == "SSB_KPI_JAE"

    # Frequency filter
    res_quarterly = get_index_series(frequency="quarterly")
    assert any(s["series_id"] == "SSB_IKT_TJENESTER" for s in res_quarterly)


def test_calculate_index_uplift_measurements() -> None:
    # 2023 (130.0) -> 2024 (134.7): (134.7 - 130.0) / 130.0 = 0.03615... (3.62%)
    calc = calculate_index_uplift(
        series_id="SSB_KPI",
        base_period="2023",
        target_period="2024",
    )
    assert calc["series_id"] == "SSB_KPI"
    assert calc["base_index"] == 130.0
    assert calc["target_index"] == 134.7
    assert 0.0360 <= calc["raw_change_pct"] <= 0.0365
    assert calc["effective_uplift_pct"] == calc["raw_change_pct"]
    assert not calc["is_clamped_cap"]
    assert not calc["is_clamped_floor"]


def test_calculate_index_uplift_regulation_ratio() -> None:
    # 80% regulation ratio on 3.615% -> ~2.89%
    calc = calculate_index_uplift(
        series_id="SSB_KPI",
        base_period="2023",
        target_period="2024",
        regulation_ratio=0.8,
    )
    assert calc["regulation_ratio"] == 0.8
    assert calc["effective_uplift_pct"] < calc["raw_change_pct"]
    expected = round(calc["raw_change_pct"] * 0.8, 4)
    assert abs(calc["effective_uplift_pct"] - expected) <= 0.0002


def test_calculate_index_uplift_cap_and_floor() -> None:
    # Cap clamped
    calc_cap = calculate_index_uplift(
        series_id="SSB_KPI",
        base_period="2020",
        target_period="2024",
        cap_pct=0.05,
    )
    assert calc_cap["raw_change_pct"] > 0.05
    assert calc_cap["effective_uplift_pct"] == 0.05
    assert calc_cap["is_clamped_cap"] is True

    # Floor clamped on negative movement
    calc_floor = calculate_index_uplift(
        series_id="SSB_KPI",
        base_index=140.0,
        target_index=130.0,
        floor_pct=0.0,
    )
    assert calc_floor["raw_change_pct"] < 0
    assert calc_floor["effective_uplift_pct"] == 0.0
    assert calc_floor["is_clamped_floor"] is True


def test_calculate_index_adjustment_with_amounts() -> None:
    res = do_calculate_index_adjustment(
        None,
        {
            "series_id": "SSB_KPI",
            "base_period": "2023",
            "target_period": "2024",
            "current_annual_amount": 100_000.0,
        },
    )
    assert res["ok"] is True
    assert res["current_amount"] == 100_000.0
    assert res["new_amount"] > 100_000.0
    assert res["new_amount"] == round(100_000.0 + res["adjustment_amount"], 2)


# ---------------------------------------------------------------------------
# 3. Price Rules Domain & Evaluation Tests
# ---------------------------------------------------------------------------


def test_get_price_rules_filters() -> None:
    rules = get_price_rules(rule_type="sla_room_pricing")
    assert len(rules) == 1
    assert rules[0]["rule_id"] == "RULE_SLA_ROOM_CATEGORY"

    rule = get_price_rules(rule_id="RULE_KPI_STANDARD")
    assert len(rule) == 1
    assert rule[0]["parameters"]["regulation_ratio"] == 1.0


def test_evaluate_cpi_index_price_rule() -> None:
    eval_res = evaluate_price_rule(
        "RULE_KPI_STANDARD",
        {
            "base_period": "2023",
            "target_period": "2024",
            "current_annual_amount": 200_000.0,
        },
    )
    assert eval_res["rule_id"] == "RULE_KPI_STANDARD"
    assert eval_res["rule_type"] == "cpi_index_regulation"
    assert "effective_uplift_pct" in eval_res
    assert eval_res["renewal_amount"] > 200_000.0


def test_evaluate_sla_room_pricing_rule() -> None:
    eval_res = evaluate_price_rule(
        "RULE_SLA_ROOM_CATEGORY",
        {
            "room_counts": {
                "standard_meeting_room": 4,  # 4 * 450 = 1800
                "large_boardroom": 2,  # 2 * 950 = 1900
            },
            "on_site": False,
        },
    )
    assert eval_res["rule_id"] == "RULE_SLA_ROOM_CATEGORY"
    assert eval_res["total_monthly_amount"] == 3700.0  # 1800 + 1900
    assert eval_res["total_annual_amount"] == 44400.0  # 3700 * 12


def test_evaluate_volume_tier_discount_rule() -> None:
    eval_res = evaluate_price_rule(
        "RULE_VOLUME_ROOMS",
        {
            "total_rooms": 30,  # 26-50 tier -> 10%
            "base_amount": 10_000.0,
        },
    )
    assert eval_res["discount_pct"] == 0.10
    assert eval_res["discount_amount"] == 1000.0
    assert eval_res["final_amount"] == 9000.0


def test_evaluate_multi_year_commitment_rule() -> None:
    eval_res = evaluate_price_rule(
        "RULE_COMMITMENT_3YR",
        {"commitment_months": 36},
    )
    assert eval_res["qualifies_for_discount"] is True
    assert eval_res["discount_pct"] == 0.08
    assert eval_res["cpi_cap_ceiling"] == 0.03


def test_evaluate_minimum_fee_floor_rule() -> None:
    # Below floor (1200 < 1500)
    eval_below = evaluate_price_rule(
        "RULE_MINIMUM_FEE_FLOOR",
        {"monthly_fee": 1200.0},
    )
    assert eval_below["is_floor_applied"] is True
    assert eval_below["final_monthly_fee"] == 1500.0

    # Above floor (2000 >= 1500)
    eval_above = evaluate_price_rule(
        "RULE_MINIMUM_FEE_FLOOR",
        {"monthly_fee": 2000.0},
    )
    assert eval_above["is_floor_applied"] is False
    assert eval_above["final_monthly_fee"] == 2000.0


# ---------------------------------------------------------------------------
# 4. MCP Handlers & Namespace Opt-in Gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agreements_mcp_handlers_success() -> None:
    engine = _make_mock_engine(agreements_enabled=True)

    # 1. get_index_series
    raw = await handle_agreements_get_index_series(
        engine, {"namespace_id": _NS_ID, "series_id": "SSB_KPI"}
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["total"] == 1

    # 2. calculate_index_adjustment
    raw = await handle_agreements_calculate_index_adjustment(
        engine,
        {
            "namespace_id": _NS_ID,
            "series_id": "SSB_KPI",
            "base_period": "2023",
            "target_period": "2024",
            "current_annual_amount": 100000,
        },
    )
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["current_amount"] == 100000.0

    # 3. get_price_rules
    raw = await handle_agreements_get_price_rules(
        engine, {"namespace_id": _NS_ID, "rule_id": "RULE_KPI_STANDARD"}
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["total"] == 1

    # 4. evaluate_price_rule
    raw = await handle_agreements_evaluate_price_rule(
        engine,
        {
            "namespace_id": _NS_ID,
            "rule_id": "RULE_KPI_STANDARD",
            "base_period": "2023",
            "target_period": "2024",
            "current_annual_amount": 100000,
        },
    )
    data = json.loads(raw)
    assert data["ok"] is True
    assert "renewal_amount" in data


@pytest.mark.asyncio
async def test_agreements_mcp_handlers_opt_in_refusal() -> None:
    engine = _make_mock_engine(agreements_enabled=False)

    with pytest.raises(McpError) as exc:
        await handle_agreements_get_index_series(engine, {"namespace_id": _NS_ID})
    assert exc.value.code == -32005

    with pytest.raises(McpError) as exc:
        await handle_agreements_get_price_rules(engine, {"namespace_id": _NS_ID})
    assert exc.value.code == -32005


# ---------------------------------------------------------------------------
# 5. Economy Validate Contract Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_economy_validate_contract_with_index_series() -> None:
    contract_row = {"annual_amount": Decimal("100000.00"), "cpi_cap": Decimal("0.05")}
    engine = _make_mock_engine(economy_contract_row=contract_row)

    res = await do_validate_contract(
        engine,
        {
            "namespace_id": _NS_ID,
            "contract_id": _CTR_ID,
            "index_series_id": "SSB_KPI",
            "base_period": "2023",
            "target_period": "2024",
        },
    )
    assert res["ok"] is True
    assert res["contract_id"] == _CTR_ID
    assert res["index_series_id"] == "SSB_KPI"
    assert 0.035 <= float(res["proposed_cpi_pct"]) <= 0.040
    assert float(res["renewal_annual_amount"]) > 100000.0


@pytest.mark.asyncio
async def test_economy_validate_contract_with_price_rule() -> None:
    contract_row = {"annual_amount": Decimal("100000.00"), "cpi_cap": Decimal("0.05")}
    engine = _make_mock_engine(economy_contract_row=contract_row)

    res = await do_validate_contract(
        engine,
        {
            "namespace_id": _NS_ID,
            "contract_id": _CTR_ID,
            "price_rule_id": "RULE_KPI_STANDARD",
            "base_period": "2023",
            "target_period": "2024",
        },
    )
    assert res["ok"] is True
    assert res["price_rule_id"] == "RULE_KPI_STANDARD"
    assert 0.035 <= float(res["proposed_cpi_pct"]) <= 0.040


@pytest.mark.asyncio
async def test_economy_validate_contract_agreements_table_fallback() -> None:
    # Not found in economy_contracts, found in agreements table (Wave B-9)
    agr_row = {
        "annual_value": Decimal("150000.00"),
        "metadata": {"cpi_cap": 0.04},
    }
    engine = _make_mock_engine(economy_contract_row=None, agreement_row=agr_row)

    res = await do_validate_contract(
        engine,
        {
            "namespace_id": _NS_ID,
            "contract_id": "AGR-2026-001",
            "proposed_cpi_pct": 0.03,
        },
    )
    assert res["ok"] is True
    assert res["contract_id"] == "AGR-2026-001"
    assert float(res["current_annual_amount"]) == 150000.00
    assert float(res["cpi_cap"]) == 0.04
    assert float(res["renewal_annual_amount"]) == 154500.00


# ---------------------------------------------------------------------------
# 6. REST Routes Tests
# ---------------------------------------------------------------------------


class _StubRequest:
    def __init__(
        self, query_params: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> None:
        self.query_params = query_params or {}
        self._body = body or {}
        self.headers = {"content-type": "application/json"}
        self.path_params = {}

    async def json(self) -> dict[str, Any]:
        return self._body


@pytest.mark.asyncio
async def test_api_agreements_price_rules_and_index_series_rest() -> None:
    engine = _make_mock_engine(agreements_enabled=True)
    admin_state.engine = engine

    # 1. GET /api/agreements/index-series
    req_idx = _StubRequest(query_params={"namespace_id": _NS_ID, "series_id": "SSB_KPI"})
    resp_idx = await agreements_admin_handlers.api_agreements_get_index_series(req_idx)
    assert resp_idx.status_code == 200
    data = json.loads(resp_idx.body)
    assert data["status"] == "ok"
    assert data["total"] == 1

    # 2. POST /api/agreements/index-series/calculate
    req_calc = _StubRequest(
        body={
            "namespace_id": _NS_ID,
            "series_id": "SSB_KPI",
            "base_period": "2023",
            "target_period": "2024",
            "current_annual_amount": 120000,
        }
    )
    resp_calc = await agreements_admin_handlers.api_agreements_calculate_index_adjustment(req_calc)
    assert resp_calc.status_code == 200
    data = json.loads(resp_calc.body)
    assert data["ok"] is True
    assert data["current_amount"] == 120000.0

    # 3. GET /api/agreements/price-rules
    req_rules = _StubRequest(query_params={"namespace_id": _NS_ID, "rule_id": "RULE_KPI_STANDARD"})
    resp_rules = await agreements_admin_handlers.api_agreements_get_price_rules(req_rules)
    assert resp_rules.status_code == 200
    data = json.loads(resp_rules.body)
    assert data["status"] == "ok"
    assert data["total"] == 1

    # 4. POST /api/agreements/price-rules/evaluate
    req_eval = _StubRequest(
        body={
            "namespace_id": _NS_ID,
            "rule_id": "RULE_VOLUME_ROOMS",
            "total_rooms": 20,
            "base_amount": 10000,
        }
    )
    resp_eval = await agreements_admin_handlers.api_agreements_evaluate_price_rule(req_eval)
    assert resp_eval.status_code == 200
    data = json.loads(resp_eval.body)
    assert data["ok"] is True
    assert data["discount_pct"] == 0.05
    assert data["final_amount"] == 9500.0
