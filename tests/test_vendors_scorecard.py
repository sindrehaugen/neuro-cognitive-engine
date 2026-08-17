"""Unit and integration tests for the Vendors Scorecard vertical module (Batch 095)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.vendors.scorecard import do_compute_scorecard, load_scorecard_weights


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(self, pg_pool: asyncpg.Pool | None = None) -> None:
        self.pg_pool = pg_pool


@pytest.mark.asyncio
async def test_scorecard_insufficient_data() -> None:
    """Verify that scorecards with sample size below min_sample return neutral/insufficient_data."""
    engine = EngineStub()
    params = {
        "namespace_id": uuid.uuid4(),
        "vendor_id": "VENDOR:ACME",
        "events": [
            {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
            {"on_time": False, "defect_rma": True, "substituted": False, "reliability": 50.0},
        ],
    }

    res = await do_compute_scorecard(engine, params)
    assert res["insufficient_data"] is True
    assert res["on_time_pct"] is None
    assert res["defect_rma_rate"] is None
    assert res["substitution_rate"] is None
    assert res["reliability"] is None
    assert res["composite_score"] is None
    assert res["sample_n"] == 2


@pytest.mark.asyncio
async def test_scorecard_calculation_with_weights() -> None:
    """Verify scorecard calculation matches weighted formula from vendor-scorecard-weights.json."""
    engine = EngineStub()

    events = [
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
        {"on_time": True, "defect_rma": True, "substituted": False, "reliability": 50.0},
        {"on_time": False, "defect_rma": False, "substituted": True, "reliability": 80.0},
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 90.0},
    ]

    params = {
        "namespace_id": uuid.uuid4(),
        "vendor_id": "VENDOR:ACME",
        "events": events,
    }

    res = await do_compute_scorecard(engine, params)
    assert res["insufficient_data"] is False
    assert res["sample_n"] == 5

    assert res["on_time_pct"] == 80.0
    assert res["defect_rma_rate"] == 20.0
    assert res["substitution_rate"] == 20.0
    assert res["reliability"] == 84.0

    weights = load_scorecard_weights()
    on_time_score = 80.0
    defect_rma_score = 100.0 - 20.0
    substitution_score = 100.0 - 20.0
    reliability_score = 84.0

    expected_composite = (
        on_time_score * weights["on_time_weight"]
        + defect_rma_score * weights["defect_rma_weight"]
        + substitution_score * weights["substitution_weight"]
        + reliability_score * weights["reliability_weight"]
    )
    assert abs(res["composite_score"] - expected_composite) < 1e-9


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scorecard_persistence_and_rls(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,
    make_namespace: Any,
) -> None:
    """Verify scorecards are persisted correctly and isolated per namespace via RLS."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    engine = EngineStub(pg_pool)

    events_a = [
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
        {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0},
    ]
    params_a = {
        "namespace_id": ns_a,
        "vendor_id": "VENDOR:ACME",
        "events": events_a,
        "current_tier": "Gold",
        "ytd_progress": 75.0,
    }

    res_a = await do_compute_scorecard(engine, params_a)
    assert res_a["insufficient_data"] is False

    params_b = {
        "namespace_id": ns_b,
        "vendor_id": "VENDOR:ACME",
        "events": [],
        "current_tier": "Bronze",
        "ytd_progress": 10.0,
    }
    res_b = await do_compute_scorecard(engine, params_b)
    assert res_b["insufficient_data"] is True

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)

        row_a = await pg_app_conn.fetchrow(
            "SELECT * FROM vendor_scorecards WHERE vendor_id = 'VENDOR:ACME'"
        )
        assert row_a is not None
        assert row_a["namespace_id"] == ns_a
        assert float(row_a["on_time_pct"]) == 100.0
        assert float(row_a["reliability"]) == 100.0
        assert row_a["current_tier"] == "Gold"
        assert float(row_a["ytd_progress"]) == 75.0
        assert row_a["sample_n"] == 5

        raw_data = json.loads(row_a["raw"])
        assert raw_data["composite_score"] == 100.0

        row_b_hidden = await pg_app_conn.fetchrow(
            "SELECT * FROM vendor_scorecards WHERE namespace_id = $1",
            ns_b,
        )
        assert row_b_hidden is None

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)

        row_b = await pg_app_conn.fetchrow(
            "SELECT * FROM vendor_scorecards WHERE vendor_id = 'VENDOR:ACME'"
        )
        assert row_b is not None
        assert row_b["namespace_id"] == ns_b
        assert row_b["on_time_pct"] is None
        assert row_b["current_tier"] == "Bronze"
        assert float(row_b["ytd_progress"]) == 10.0
        assert row_b["sample_n"] == 0

        row_a_hidden = await pg_app_conn.fetchrow(
            "SELECT * FROM vendor_scorecards WHERE namespace_id = $1",
            ns_a,
        )
        assert row_a_hidden is None
