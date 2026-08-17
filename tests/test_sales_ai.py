"""Integration tests for Sales AI Surface (Batch 091 — ai-surface)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.embeddings import embed
from nce.orchestrator import NCEEngine
from nce.vertical_modules.sales.ai import (
    do_draft_quote,
    do_record_ai_decision,
    do_score_lead,
    do_win_loss_recall,
)


def _make_engine_stub(pg_pool: asyncpg.Pool) -> NCEEngine:
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub  # type: ignore[return-value]


def _orthogonal_vec(dim: int = 768) -> list[float]:
    v = [0.0] * dim
    v[-1] = 1.0
    return v


async def _seed_memory(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
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
        "test-ai-agent",
        json.dumps(embedding),
        payload_ref,
        json.dumps(metadata),
        node_type,
        name,
    )
    return mem_id


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesAiSurface:
    """Integration tests for Sales AI features."""

    async def test_win_loss_recall_and_lead_scoring(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify do_win_loss_recall ranks similar deals, and do_score_lead scores correctly (propose-only)."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # 1. Generate query vector
        query_text = "enterprise backup solutions for banking sector"
        query_vec = await embed(query_text)

        # 2. Seed memory data
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)

                # Seed 3 similar deals (2 won, 1 lost)
                await _seed_memory(
                    conn,
                    ns_id=ns,
                    embedding=query_vec,  # Dist 0
                    node_type="DEAL",
                    name="Won deal A",
                    metadata={"status": "won", "outcome": "won", "value": 500000},
                )
                await _seed_memory(
                    conn,
                    ns_id=ns,
                    embedding=query_vec,  # Dist 0
                    node_type="DEAL",
                    name="Won deal B",
                    metadata={"status": "won", "outcome": "won", "value": 450000},
                )
                await _seed_memory(
                    conn,
                    ns_id=ns,
                    embedding=query_vec,  # Dist 0
                    node_type="DEAL",
                    name="Lost deal C",
                    metadata={"status": "lost", "outcome": "lost", "value": 600000},
                )

                # Seed 1 completely different deal
                await _seed_memory(
                    conn,
                    ns_id=ns,
                    embedding=_orthogonal_vec(),  # Orthogonal vector (large distance)
                    node_type="DEAL",
                    name="Orthogonal lost deal",
                    metadata={"status": "lost", "outcome": "lost", "value": 20000},
                )

        # 3. Test do_win_loss_recall retrieves top-K and ranks by similarity
        recall_res = await do_win_loss_recall(
            engine,
            {
                "namespace_id": ns,
                "query_text": query_text,
                "top_k": 3,
            },
        )
        assert recall_res["ok"] is True
        candidates = recall_res["candidates"]
        assert len(candidates) == 3
        # First candidates should be the similar ones (cosine similarity near 1.0)
        for cand in candidates:
            assert cand["similarity"] > 0.9

        # 4. Test do_score_lead scores correctly (2 won / 3 similar = 0.666...)
        score_res = await do_score_lead(
            engine,
            {
                "namespace_id": ns,
                "query_text": query_text,
            },
        )
        assert score_res["ok"] is True
        assert abs(score_res["score"] - 0.666666) < 0.05
        assert score_res["confidence"] > 0.9
        assert score_res["propose_only"] is True
        assert len(score_res["reasons"]) > 0

    async def test_quote_draft_assist_propose_only(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify do_draft_quote proposes correct lines and margin (propose-only, validated=False)."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        query_text = "Standard Audio visual kit for meeting room"
        query_vec = await embed(query_text)

        # Seed historical won quote in memories
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _seed_memory(
                    conn,
                    ns_id=ns,
                    embedding=query_vec,
                    node_type="QUOTE",
                    name="Won Meeting Room Quote",
                    metadata={
                        "signed_margin_pct": 0.40,
                        "lines": [
                            {"product_ref": "prod-av-1", "qty": 2, "unit_price": 500.0},
                            {"product_ref": "prod-av-2", "qty": 1, "unit_price": 2000.0},
                        ],
                    },
                )

        draft_res = await do_draft_quote(
            engine,
            {
                "namespace_id": ns,
                "description": query_text,
                "opportunity_id": "opp-123",
            },
        )
        assert draft_res["ok"] is True
        assert draft_res["propose_only"] is True
        assert draft_res["validated"] is False
        assert draft_res["suggested_margin_pct"] == 0.40
        assert len(draft_res["proposed_lines"]) == 2
        p_refs = {x["product_ref"] for x in draft_res["proposed_lines"]}
        assert p_refs == {"prod-av-1", "prod-av-2"}

    async def test_record_ai_decision_appends_to_ledger(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify do_record_ai_decision appends a sales_ai_decision event to event_log."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        decision_res = await do_record_ai_decision(
            engine,
            {
                "namespace_id": ns,
                "agent_id": "lead-scorer",
                "decision_type": "lead_score_override",
                "decision_details": {
                    "lead_id": "lead-456",
                    "original_score": 0.35,
                    "overridden_score": 0.80,
                    "reason": "Customer is a high-profile partner with strategic value",
                },
            },
        )
        assert decision_res["ok"] is True
        assert decision_res["status"] == "logged"
        event_id = decision_res["event_id"]
        assert event_id is not None

        # Verify the event is in the database event_log
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                row = await conn.fetchrow(
                    "SELECT event_type, agent_id, params FROM event_log WHERE id = $1",
                    uuid.UUID(event_id),
                )
                assert row is not None
                assert row["event_type"] == "sales_ai_decision"
                assert row["agent_id"] == "lead-scorer"
                params = (
                    json.loads(row["params"]) if isinstance(row["params"], str) else row["params"]
                )
                assert params["decision_type"] == "lead_score_override"
                assert params["details"]["lead_id"] == "lead-456"
