"""
tests/test_vendors_procurement_feed.py
=======================================
Integration tests for Batch 098 (Module 4 Wave 5).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.procurement.mcp_handlers import handle_procurement_rank_suppliers
from nce.vertical_modules.vendors.feed import (
    do_check_tier_at_risk,
    do_detect_reliability_degradation,
)
from nce.vertical_modules.vendors.tiers import do_record_outcome


class MockEngine:
    def __init__(self, pg_pool: Any, mongo_client: Any = None) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client or MagicMock()


class MockA2AClient:
    """Mock A2A Client that tracks calls and returns predefined responses."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> Any:
        self.calls.append((tool_name, params))
        if tool_name in self.responses:
            return self.responses[tool_name]
        raise ValueError(f"No mock response for tool: {tool_name}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_procurement_rank_suppliers_enrichment_a2a(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify handle_procurement_rank_suppliers enriches candidates via A2A client."""
    engine = MockEngine(pg_pool)

    # 1. Define mock A2A responses
    mock_responses = {
        "vendors_get_tier_status": {
            "vendor_id": "VENDOR:ACME",
            "current_tier": "Platinum",
            "ytd_volume": 300000.0,
            "next_tier_threshold": None,
        },
        "vendors_get_vendor": {
            "label": "VENDOR:ACME",
            "scorecard": {
                "reliability": 90.0,
                "sample_n": 10,
            },
        },
    }
    a2a_client = MockA2AClient(mock_responses)

    # 2. Call handler with A2A client
    arguments = {
        "namespace_id": str(namespace_id),
        "a2a_client": a2a_client,
        "bom_line": {"quantity": 10},
        "candidates": [
            {"supplier_id": "VENDOR:ACME", "unit_price": 100.0},
        ],
    }

    result_json = await handle_procurement_rank_suppliers(engine, arguments)  # type: ignore[arg-type]
    result = json.loads(result_json)

    # 3. Assertions
    assert "ranked" in result
    ranked_candidates = result["ranked"]
    assert len(ranked_candidates) == 1
    enriched_candidate = ranked_candidates[0]

    # Platinum maps to tier 1
    assert enriched_candidate["supplier_tier"] == 1
    # Scorecard reliability 90.0 maps to 0.9
    assert enriched_candidate["delivery_reliability"] == 0.9
    # Verify A2A calls were tracked
    assert len(a2a_client.calls) == 2
    assert a2a_client.calls[0][0] == "vendors_get_tier_status"
    assert a2a_client.calls[1][0] == "vendors_get_vendor"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_procurement_rank_suppliers_enrichment_direct_fallback(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify handle_procurement_rank_suppliers falls back to direct handlers."""
    engine = MockEngine(pg_pool)

    # 1. Seed vendor in kg_nodes
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ('VENDOR:DIRECT', 'VENDOR', $1, 'agent')
            """,
            namespace_id,
        )

    # 2. Seed outcome events in cognitive ledger to build scorecard
    # We need at least 5 events to avoid insufficient_data
    for _ in range(5):
        await do_record_outcome(
            engine,
            {
                "namespace_id": namespace_id,
                "event_type": "match_decision",
                "vendor_id": "VENDOR:DIRECT",
                "decision": "accept",
                "amount": 2000.0,  # total YTD = 10,000 (Bronze threshold)
                "score": 90.0,
            },
        )

    # Trigger a scorecard compute to persist it to vendor_scorecards
    from nce.vertical_modules.vendors.scorecard import do_compute_scorecard

    await do_compute_scorecard(
        engine,
        {
            "namespace_id": namespace_id,
            "vendor_id": "VENDOR:DIRECT",
            "current_tier": "Bronze",
            "ytd_progress": 0.0,
            "events": [
                {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 80.0}
            ]
            * 5,
        },
    )

    # 3. Call handler without A2A client (triggering fallback)
    arguments = {
        "namespace_id": str(namespace_id),
        "bom_line": {"quantity": 10},
        "candidates": [
            {"supplier_id": "VENDOR:DIRECT", "unit_price": 120.0},
        ],
    }

    result_json = await handle_procurement_rank_suppliers(engine, arguments)  # type: ignore[arg-type]
    result = json.loads(result_json)

    # 4. Assertions
    ranked_candidates = result["ranked"]
    assert len(ranked_candidates) == 1
    enriched_candidate = ranked_candidates[0]

    # Bronze maps to tier 4
    assert enriched_candidate["supplier_tier"] == 4
    # Reliability 80.0 maps to 0.8
    assert enriched_candidate["delivery_reliability"] == 0.8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reliability_degradation_watcher(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify do_detect_reliability_degradation flags downward performance trend."""
    engine = MockEngine(pg_pool)

    # 1. Seed vendor
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ('VENDOR:DEGRADE', 'VENDOR', $1, 'agent')
            """,
            namespace_id,
        )

    # 2. Seed historical outcomes directly (good performance: on-time, no defects)
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        for _ in range(3):
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (
                    id, namespace_id, empathic_tensor, tlx_scores, vad_scores, model_version
                ) VALUES (
                    $1::uuid, $2::uuid, $3::float[], $4::jsonb, $5::jsonb, $6
                )
                """,
                uuid.uuid4(),
                namespace_id,
                [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                json.dumps(
                    {
                        "event_type": "match_decision",
                        "vendor_id": "VENDOR:DEGRADE",
                        "on_time": True,
                        "defect_rma": False,
                        "reliability": 100.0,
                    }
                ),
                json.dumps({}),
                "test-outcome-1.0",
            )

        # 3. Seed recent outcomes directly (bad performance: late, high defects)
        for _ in range(3):
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (
                    id, namespace_id, empathic_tensor, tlx_scores, vad_scores, model_version
                ) VALUES (
                    $1::uuid, $2::uuid, $3::float[], $4::jsonb, $5::jsonb, $6
                )
                """,
                uuid.uuid4(),
                namespace_id,
                [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                json.dumps(
                    {
                        "event_type": "match_decision",
                        "vendor_id": "VENDOR:DEGRADE",
                        "on_time": False,
                        "defect_rma": True,
                        "reliability": 20.0,
                    }
                ),
                json.dumps({}),
                "test-outcome-1.0",
            )

    # 4. Call Watcher (min_sample=6)
    res = await do_detect_reliability_degradation(
        engine,
        {
            "namespace_id": namespace_id,
            "vendor_id": "VENDOR:DEGRADE",
            "min_sample": 6,
            "threshold": 10.0,
        },
    )

    assert res["degraded"] is True
    assert res["historical_on_time_pct"] == 100.0
    assert res["recent_on_time_pct"] == 0.0
    assert res["on_time_degraded_pct"] == 100.0
    assert res["defect_degraded_pct"] == 100.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier_at_risk_watcher(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify do_check_tier_at_risk identifies at-risk rebate targets."""
    engine = MockEngine(pg_pool)

    # Seed vendor
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ('VENDOR:AT_RISK', 'VENDOR', $1, 'agent')
            """,
            namespace_id,
        )

    # 1. At risk scenario: volume = 50,000, threshold = 250,000, days left = 180
    res_at_risk = await do_check_tier_at_risk(
        engine,
        {
            "namespace_id": namespace_id,
            "vendor_id": "VENDOR:AT_RISK",
            "ytd_volume": 50000.0,
            "next_tier_threshold": 250000.0,
            "days_left": 180,
        },
    )
    assert res_at_risk["at_risk"] is True

    # 2. Safe scenario: volume = 200,000, threshold = 250,000, days left = 180
    res_safe = await do_check_tier_at_risk(
        engine,
        {
            "namespace_id": namespace_id,
            "vendor_id": "VENDOR:AT_RISK",
            "ytd_volume": 200000.0,
            "next_tier_threshold": 250000.0,
            "days_left": 180,
        },
    )
    assert res_safe["at_risk"] is False
