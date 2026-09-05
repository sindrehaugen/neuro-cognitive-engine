"""Integration tests for Sales Commission and A2A Flow (Batch 092 — commission-a2a)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.embeddings import embed
from nce.event_log import append_event
from nce.orchestrator import NCEEngine
from nce.vertical_modules.sales.commission import (
    do_calculate_commission,
    do_initiate_quote_flow,
    do_record_deal_loss_feedback,
    load_commission_config,
)


def _make_engine_stub(pg_pool: asyncpg.Pool) -> NCEEngine:
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub  # type: ignore[return-value]


async def _seed_memory(
    conn: asyncpg.Connection,
    *,
    ns_id: uuid.UUID,
    embedding: list[float],
    node_type: str,
    name: str,
    metadata: dict[str, Any],
) -> uuid.UUID:
    mem_id = uuid.uuid4()
    payload_ref = mem_id.hex[:24]
    await conn.execute(
        """
        INSERT INTO memories
            (id, namespace_id, agent_id, embedding, assertion_type,
             memory_type, payload_ref, metadata, node_type, name,
             change_origin)
        VALUES ($1, $2::uuid, $3, $4::vector, 'fact', 'episodic',
                $5, $6::jsonb, $7, $8, 'sync')
        """,
        mem_id,
        ns_id,
        "test-sales-commission-agent",
        json.dumps(embedding),
        payload_ref,
        json.dumps(metadata),
        node_type,
        name,
    )
    return mem_id


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesCommissionAndA2A:
    """Integration tests for Sales commission calculation, A2A flow, and feedback loops."""

    async def test_reproducible_commission(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify commission calculation is reproducible from both direct parameters and ledger events."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # 1. Verify config load
        config = load_commission_config()
        assert config.get("version") == "1.0"
        assert len(config.get("tiers", [])) > 0

        # 2. Test direct calculation via do_calculate_commission
        deal_data = {
            "quote_id": "QUOTE-COMP-101",
            "seller_id": "SELLER-BOB",
            "items": [
                {"sku": "SKU-HW-1", "type": "hardware", "price": 100000.0, "cost": 75000.0},
                {"sku": "SKU-SV-1", "type": "service", "price": 50000.0, "cost": 30000.0},
            ],
        }
        # Margin: (150k - 105k) / 150k = 45k / 150k = 30%.
        # 30% margin is in Tier 2 (min 20%, max 35%), rate: hardware=0.02, service=0.04.
        # Hardware profit: 25,000 * 0.02 = 500.
        # Service profit: 20,000 * 0.04 = 800.
        # Total expected commission = 1300.0.

        res_direct = await do_calculate_commission(
            engine, {"namespace_id": str(ns), "deal_data": deal_data}
        )
        assert res_direct.get("ok") is True
        assert abs(res_direct.get("commission", 0.0) - 1300.0) < 1e-5

        # 3. Test calculation from ledger events
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)

                # Log event for QUOTE-COMP-101
                await append_event(
                    conn=conn,
                    namespace_id=ns,
                    agent_id="test-sales-agent",
                    event_type="sales_ai_decision",
                    params={"decision_type": "deal_won", "details": deal_data},
                )

        # Calculate commission for SELLER-BOB from ledger
        res_ledger = await do_calculate_commission(
            engine, {"namespace_id": str(ns), "seller_id": "SELLER-BOB"}
        )
        assert res_ledger.get("ok") is True
        assert abs(res_ledger.get("total_commission", 0.0) - 1300.0) < 1e-5
        assert len(res_ledger.get("commissions", [])) == 1
        assert res_ledger["commissions"][0]["quote_id"] == "QUOTE-COMP-101"

    async def test_a2a_quote_design_procure_flow(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify the A2A Quote->Design->Procure flow triggers design propose, product enrich, and tco calc."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # Seed a DESIGN memory in the DB so do_propose_design has something to recall
        query_text = "high end rack setup"
        query_vec = await embed(query_text)

        # Seed product catalog row and design memory
        product_id = str(uuid.uuid4())
        # product_catalog is global now: (manufacturer, mfr_part_no) is the
        # unique identity, so a fixed literal would collide across runs.
        part_no = f"PART-{product_id[:8]}"

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                # Seed product catalog so that do_enrich_product succeeds
                await conn.execute(
                    """
                    INSERT INTO product_catalog (id, manufacturer, mfr_part_no, product_source_id)
                    VALUES ($1::uuid, 'Test Mfg', $2, $3)
                    """,
                    uuid.UUID(product_id),
                    part_no,
                    f"src-{part_no}",
                )
                await _seed_memory(
                    conn,
                    ns_id=ns,
                    embedding=query_vec,
                    node_type="DESIGN",
                    name="Master Rack Design",
                    metadata={
                        "outcome": "won",
                        "product_ref": "SKU-RACK-01",
                        "qty": 1,
                    },
                )

        # Initiate A2A flow
        res_flow = await do_initiate_quote_flow(
            engine,
            {
                "namespace_id": str(ns),
                "query_text": query_text,
                "product_id": product_id,
            },
        )

        assert res_flow.get("ok") is True

        # Verify System Design output
        assert "system_design_proposal" in res_flow
        assert "proposed_lines" in res_flow["system_design_proposal"]

        # Verify Product Enrichment output
        assert "product_enrichment" in res_flow
        assert res_flow["product_enrichment"].get("product_id") == product_id
        assert res_flow["product_enrichment"].get("enrichment") == "queued"

        # Verify Procurement TCO output
        assert "procurement_tco" in res_flow
        assert "total" in res_flow["procurement_tco"]
        assert res_flow["procurement_tco"].get("total") is not None

    async def test_failure_pattern_feedback_edge(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify that do_record_deal_loss_feedback successfully writes/upserts feedback edges to the KG."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        sku = "SKU-FAULTY-SCREEN"
        loss_reason = "fragile panel"

        res = await do_record_deal_loss_feedback(
            engine,
            {
                "namespace_id": str(ns),
                "sku": sku,
                "loss_reason": loss_reason,
                "confidence": 0.85,
            },
        )

        assert res.get("ok") is True
        assert res["edge"]["subject_label"] == f"PRODUCT:{sku}"
        assert res["edge"]["predicate"] == "failure_pattern"
        assert res["edge"]["object_label"] == f"LOSS_REASON:{loss_reason.upper()}"

        # Verify database record
        async with pg_pool.acquire() as conn:
            await set_namespace_context(conn, ns)
            row = await conn.fetchrow(
                """
                SELECT confidence, change_origin
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND subject_label = $2
                  AND predicate = 'failure_pattern'
                  AND object_label = $3
                """,
                ns,
                f"PRODUCT:{sku}",
                f"LOSS_REASON:{loss_reason.upper()}",
            )
            assert row is not None
            assert abs(row["confidence"] - 0.85) < 1e-5
            assert row["change_origin"] == "agent"
