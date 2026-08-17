"""
tests/test_vendors_tiers.py
===========================
Integration tests for Batch 097 — Module 4.Wave 4 (tiers-outcomes).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.vendors.tiers import (
    do_get_tier_status,
    do_record_outcome,
    strip_tier_details,
)


class MockEngine:
    def __init__(self, pg_pool: Any, mongo_client: Any = None) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client or MagicMock()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_outcome_procurement_and_contractor(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify do_record_outcome appends outcomes to v3_cognitive_ledger."""
    engine = MockEngine(pg_pool)

    # 1. Record vendor procurement match outcome
    res_vendor = await do_record_outcome(
        engine,
        {
            "namespace_id": namespace_id,
            "event_type": "match_decision",
            "vendor_id": "VENDOR:ACME",
            "decision": "accept",
            "score": 95.5,
            "amount": 10500.0,
        },
    )
    assert res_vendor["ok"] is True
    assert "ledger_id" in res_vendor
    assert res_vendor["event_type"] == "match_decision"

    # 2. Record contractor work order rating
    res_contractor = await do_record_outcome(
        engine,
        {
            "namespace_id": namespace_id,
            "event_type": "work_order_rating",
            "contractor_id": "CONTRACTOR:BOB",
            "rating": 5.0,
            "work_order_id": "WO-001",
        },
    )
    assert res_contractor["ok"] is True
    assert "ledger_id" in res_contractor
    assert res_contractor["event_type"] == "work_order_rating"

    # 3. Retrieve from database to verify contents
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        rows = await conn.fetch(
            "SELECT tlx_scores FROM v3_cognitive_ledger WHERE namespace_id = $1 ORDER BY created_at DESC",
            namespace_id,
        )
        assert len(rows) >= 2
        payloads = [json.loads(r["tlx_scores"]) for r in rows]

        # Verify contractor rating payload
        rating_payload = next(p for p in payloads if p["event_type"] == "work_order_rating")
        assert rating_payload["contractor_id"] == "CONTRACTOR:BOB"
        assert rating_payload["rating"] == 5.0
        assert rating_payload["work_order_id"] == "WO-001"

        # Verify vendor match payload
        match_payload = next(p for p in payloads if p["event_type"] == "match_decision")
        assert match_payload["vendor_id"] == "VENDOR:ACME"
        assert match_payload["decision"] == "accept"
        assert match_payload["score"] == 95.5
        assert match_payload["amount"] == 10500.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_tier_status_default_tiers(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify do_get_tier_status computes correct default tiers and progress."""
    engine = MockEngine(pg_pool)

    # 1. Seed VENDOR node in kg_nodes
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ('VENDOR:ACME', 'VENDOR', $1, 'agent')
            """,
            namespace_id,
        )

    # 2. Record some outcomes with different amounts to check YTD volume summing
    # Outcome 1: volume 12,000 (exceeds Bronze threshold 10,000)
    await do_record_outcome(
        engine,
        {
            "namespace_id": namespace_id,
            "event_type": "match_decision",
            "vendor_id": "VENDOR:ACME",
            "decision": "accept",
            "amount": 12000.0,
        },
    )
    # Outcome 2: volume 30,000 (total volume = 42,000, which is in Bronze progressing to Silver)
    await do_record_outcome(
        engine,
        {
            "namespace_id": namespace_id,
            "event_type": "match_decision",
            "vendor_id": "VENDOR:ACME",
            "decision": "accept",
            "amount": 30000.0,
        },
    )

    # 3. Retrieve tier status
    status = await do_get_tier_status(
        engine,
        {
            "namespace_id": namespace_id,
            "vendor_id": "VENDOR:ACME",
        },
    )

    assert status["vendor_id"] == "VENDOR:ACME"
    assert status["current_tier"] == "Bronze"
    assert status["ytd_volume"] == 42000.0
    assert status["next_tier_threshold"] == 50000.0  # Silver threshold
    # Bronze (10k) -> Silver (50k): progress = (42k - 10k) / (50k - 10k) = 32k / 40k = 0.8
    assert abs(status["ytd_progress"] - 0.8) < 1e-5
    assert status["days_left"] >= 0

    # 4. Verify scorecard table was updated with correct status
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        scorecard = await conn.fetchrow(
            "SELECT current_tier, ytd_progress FROM vendor_scorecards WHERE vendor_id = 'VENDOR:ACME' AND namespace_id = $1",
            namespace_id,
        )
        assert scorecard is not None
        assert scorecard["current_tier"] == "Bronze"
        assert abs(float(scorecard["ytd_progress"]) - 0.8) < 1e-5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_tier_status_custom_agreement_tiers(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify do_get_tier_status resolves custom tiers from MongoDB agreements."""
    engine = MockEngine(pg_pool)

    # 1. Seed nodes and edges
    # VENDOR node, AGREEMENT node, and VENDOR -[under]-> AGREEMENT edge
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin, payload_ref)
            VALUES ('VENDOR:ACME_CUSTOM', 'VENDOR', $1, 'agent', '000000000000000000000000')
            """,
            namespace_id,
        )
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin, payload_ref)
            VALUES ('AGREEMENT:ACME_AGR', 'AGREEMENT', $1, 'agent', '60b8d2b2f1d2b2f1d2f1d2f1')
            """,
            namespace_id,
        )
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, namespace_id, change_origin)
            VALUES ('VENDOR:ACME_CUSTOM', 'under', 'AGREEMENT:ACME_AGR', $1, 'agent')
            """,
            namespace_id,
        )

    # Custom tiers definition:
    # Tier A: threshold = 20,000
    # Tier B: threshold = 80,000
    custom_agreement_doc = {
        "kickback_tiers": [
            {"tier": "TierA", "threshold": 20000.0, "pct": 1.5},
            {"tier": "TierB", "threshold": 80000.0, "pct": 3.0},
        ]
    }

    # Mock MongoDB scoped session to return custom tiers doc
    class MockMongoDb:
        def __init__(self, doc: dict[str, Any]) -> None:
            class MockCollection:
                def __init__(self, d: dict[str, Any]) -> None:
                    self.d = d

                async def find_one(self, filter: dict[str, Any]) -> dict[str, Any] | None:
                    return self.d

            self.episodes = MockCollection(doc)

    class MockMongoContext:
        async def __aenter__(self) -> MockMongoDb:
            return MockMongoDb(custom_agreement_doc)

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

    # 2. Record YTD volume outcome: total 50,000 (which is in TierA progressing to TierB)
    await do_record_outcome(
        engine,
        {
            "namespace_id": namespace_id,
            "event_type": "match_decision",
            "vendor_id": "VENDOR:ACME_CUSTOM",
            "decision": "accept",
            "amount": 50000.0,
        },
    )

    with patch(
        "nce.vertical_modules.vendors.tiers.scoped_mongo_session",
        return_value=MockMongoContext(),
    ):
        status = await do_get_tier_status(
            engine,
            {
                "namespace_id": namespace_id,
                "vendor_id": "VENDOR:ACME_CUSTOM",
            },
        )

    assert status["vendor_id"] == "VENDOR:ACME_CUSTOM"
    assert status["current_tier"] == "TierA"
    assert status["ytd_volume"] == 50000.0
    assert status["next_tier_threshold"] == 80000.0
    # TierA (20k) -> TierB (80k): progress = (50k - 20k) / (80k - 20k) = 30k / 60k = 0.5
    assert abs(status["ytd_progress"] - 0.5) < 1e-5


def test_strip_tier_details_utility() -> None:
    """Verify strip_tier_details removes sensitive tier info from projections."""
    vendor_projection = {
        "id": "vendor-uuid",
        "label": "VENDOR:ACME",
        "name": "ACME Corp",
        "current_tier": "Gold",
        "ytd_volume": 125000.0,
        "next_tier_threshold": 250000.0,
        "ytd_progress": 0.5,
        "days_left": 180,
        "scorecard": {
            "reliability": 98.5,
            "current_tier": "Gold",
            "ytd_progress": 0.5,
            "sample_n": 12,
        },
    }

    redacted = strip_tier_details(vendor_projection)

    # 1. Assert keys removed from root
    assert "current_tier" not in redacted
    assert "ytd_volume" not in redacted
    assert "next_tier_threshold" not in redacted
    assert "ytd_progress" not in redacted
    assert "days_left" not in redacted

    # 2. Assert keys preserved at root
    assert redacted["id"] == "vendor-uuid"
    assert redacted["label"] == "VENDOR:ACME"
    assert redacted["name"] == "ACME Corp"

    # 3. Assert keys removed from nested scorecard
    assert "current_tier" not in redacted["scorecard"]
    assert "ytd_progress" not in redacted["scorecard"]
    assert redacted["scorecard"]["reliability"] == 98.5
    assert redacted["scorecard"]["sample_n"] == 12
