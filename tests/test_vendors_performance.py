"""
tests/test_vendors_performance.py
==================================
Integration tests for Batch 103 — contractor performance and similar-jobs recall.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest

from nce.auth import set_namespace_context
from nce.embeddings import _deterministic_hash_embedding
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.vendors.contractors import do_upsert_contractor
from nce.vertical_modules.vendors.performance import (
    do_compute_performance,
    do_recall_similar_jobs,
)
from nce.vertical_modules.vendors.tiers import do_record_outcome


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool
    return stub


async def mock_embed(text: str) -> list[float]:
    """Offline deterministic mock for embed using SHA-256 hash."""
    return _deterministic_hash_embedding(text)


@pytest.mark.integration
@pytest.mark.asyncio
class TestVendorsPerformance:
    """Integration test suite for contractor performance and recall."""

    @pytest.fixture(autouse=True)
    def patch_embed(self):
        """Ensure all embedding requests use the deterministic hash mock."""
        with patch("nce.vertical_modules.vendors.performance.embed", side_effect=mock_embed):
            yield

    async def test_do_compute_performance(self, pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
        """Verify performance score calculation from ratings and profile updates."""
        ns_uuid = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        contractor_id = "CONTRACTOR:PERF_TEST_01"
        partner_scope_id = uuid.uuid4()

        # Seed node ownership for the vendors engine
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_uuid)
                await seed_node_ownership_registry(conn, ns_uuid)

        # 1. Upsert a contractor profile first
        upsert_res = await do_upsert_contractor(
            engine,
            {
                "namespace_id": ns_uuid,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope_id,
                "profile": {"name": "Test Contractor"},
                "skills": ["dsp"],
            },
        )
        assert upsert_res["ok"] is True

        # 2. Record ratings in the ledger
        # We record 5 ratings to satisfy the default min_sample threshold of 5.
        # Ratings: 5.0, 4.0, 5.0, 4.0, 5.0 -> average = 4.6 -> score = 92.0
        ratings = [5.0, 4.0, 5.0, 4.0, 5.0]
        for idx, rating in enumerate(ratings):
            rec_res = await do_record_outcome(
                engine,
                {
                    "namespace_id": ns_uuid,
                    "event_type": "work_order_rating",
                    "contractor_id": contractor_id,
                    "rating": rating,
                    "work_order_id": f"WO-{idx:03d}",
                },
            )
            assert rec_res["ok"] is True

        # 3. Compute performance
        perf_res = await do_compute_performance(
            engine,
            {
                "namespace_id": ns_uuid,
                "contractor_id": contractor_id,
            },
        )
        assert perf_res["ok"] is True
        assert perf_res["contractor_id"] == contractor_id
        assert perf_res["performance_score"] == 92.0
        assert perf_res["sample_n"] == 5
        assert perf_res["insufficient_data"] is False

        # 4. Verify DB was updated
        async with pg_pool.acquire() as conn:
            await set_namespace_context(conn, ns_uuid)
            row = await conn.fetchrow(
                "SELECT performance_score FROM contractor_profiles WHERE contractor_id = $1 AND namespace_id = $2",
                contractor_id,
                ns_uuid,
            )
            assert row is not None
            assert float(row["performance_score"]) == 92.0

    async def test_compute_performance_insufficient_data(
        self, pg_pool: asyncpg.Pool, make_namespace: Any
    ) -> None:
        """Verify performance recompute with insufficient sample size returns neutral."""
        ns_uuid = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        contractor_id = "CONTRACTOR:PERF_TEST_02"
        partner_scope_id = uuid.uuid4()

        # Seed node ownership for the vendors engine
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_uuid)
                await seed_node_ownership_registry(conn, ns_uuid)

        # Seed profile
        await do_upsert_contractor(
            engine,
            {
                "namespace_id": ns_uuid,
                "contractor_id": contractor_id,
                "partner_scope_id": partner_scope_id,
                "performance_score": 85.0,
            },
        )

        # Record only 2 ratings (min_sample is 5)
        for rating in [5.0, 5.0]:
            await do_record_outcome(
                engine,
                {
                    "namespace_id": ns_uuid,
                    "event_type": "work_order_rating",
                    "contractor_id": contractor_id,
                    "rating": rating,
                },
            )

        # Compute performance
        perf_res = await do_compute_performance(
            engine,
            {
                "namespace_id": ns_uuid,
                "contractor_id": contractor_id,
            },
        )
        assert perf_res["ok"] is True
        assert perf_res["performance_score"] is None
        assert perf_res["insufficient_data"] is True
        assert perf_res["sample_n"] == 2

        # Verify DB field was set to NULL
        async with pg_pool.acquire() as conn:
            await set_namespace_context(conn, ns_uuid)
            row = await conn.fetchrow(
                "SELECT performance_score FROM contractor_profiles WHERE contractor_id = $1 AND namespace_id = $2",
                contractor_id,
                ns_uuid,
            )
            assert row is not None
            assert row["performance_score"] is None

    async def test_do_recall_similar_jobs(self, pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
        """Verify similar jobs are recalled and ranked by embedding similarity."""
        ns_uuid = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        contractor_id = "CONTRACTOR:BOB"

        # Seed memories and cognitive ledger entries
        # Memory 1: DSP crossovers
        mem1_id = uuid.uuid4()
        desc1 = "Designing a DSP digital crossover filter for active speaker enclosures."
        vec1 = await mock_embed(desc1)
        vec1_json = json.dumps(vec1)
        payload_ref1 = mem1_id.hex[:24]

        # Memory 2: Analog wiring
        mem2_id = uuid.uuid4()
        desc2 = "Analog wiring for basic home theater setup with passive speakers."
        vec2 = await mock_embed(desc2)
        vec2_json = json.dumps(vec2)
        payload_ref2 = mem2_id.hex[:24]

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_uuid)

                # Seed memory 1
                await conn.execute(
                    """
                    INSERT INTO memories (
                        id, namespace_id, agent_id, content_fts,
                        payload_ref, memory_type, assertion_type,
                        embedding, pii_redacted, metadata, node_type, name
                    ) VALUES (
                        $1::uuid, $2::uuid, 'test', to_tsvector('english', $3),
                        $4, 'episodic', 'observation',
                        $5::vector, false, $6::jsonb, 'CONTRACTOR_JOB', 'WO-X01'
                    )
                    """,
                    mem1_id,
                    ns_uuid,
                    desc1,
                    payload_ref1,
                    vec1_json,
                    json.dumps({"contractor_id": contractor_id, "description": desc1}),
                )
                # Seed ledger outcome 1
                await conn.execute(
                    """
                    INSERT INTO v3_cognitive_ledger (
                        id, memory_id, namespace_id, empathic_tensor, tlx_scores, vad_scores, model_version
                    ) VALUES (
                        $1::uuid, $2::uuid, $3::uuid, $4::float[], $5::jsonb, '{}', 'test'
                    )
                    """,
                    uuid.uuid4(),
                    mem1_id,
                    ns_uuid,
                    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    json.dumps(
                        {
                            "event_type": "work_order_rating",
                            "contractor_id": contractor_id,
                            "rating": 5.0,
                            "work_order_id": "WO-X01",
                            "description": desc1,
                        }
                    ),
                )

                # Seed memory 2
                await conn.execute(
                    """
                    INSERT INTO memories (
                        id, namespace_id, agent_id, content_fts,
                        payload_ref, memory_type, assertion_type,
                        embedding, pii_redacted, metadata, node_type, name
                    ) VALUES (
                        $1::uuid, $2::uuid, 'test', to_tsvector('english', $3),
                        $4, 'episodic', 'observation',
                        $5::vector, false, $6::jsonb, 'CONTRACTOR_JOB', 'WO-X02'
                    )
                    """,
                    mem2_id,
                    ns_uuid,
                    desc2,
                    payload_ref2,
                    vec2_json,
                    json.dumps({"contractor_id": "CONTRACTOR:ALICE", "description": desc2}),
                )
                # Seed ledger outcome 2
                await conn.execute(
                    """
                    INSERT INTO v3_cognitive_ledger (
                        id, memory_id, namespace_id, empathic_tensor, tlx_scores, vad_scores, model_version
                    ) VALUES (
                        $1::uuid, $2::uuid, $3::uuid, $4::float[], $5::jsonb, '{}', 'test'
                    )
                    """,
                    uuid.uuid4(),
                    mem2_id,
                    ns_uuid,
                    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    json.dumps(
                        {
                            "event_type": "work_order_rating",
                            "contractor_id": "CONTRACTOR:ALICE",
                            "rating": 4.0,
                            "work_order_id": "WO-X02",
                            "description": desc2,
                        }
                    ),
                )

        # 1. Query for crossover-related design - should rank Memory 1 first
        recall_res = await do_recall_similar_jobs(
            engine,
            {
                "namespace_id": ns_uuid,
                "query": "active speaker DSP crossover filters",
            },
        )
        assert len(recall_res) == 2
        assert recall_res[0]["memory_id"] == str(mem1_id)
        assert recall_res[0]["contractor_id"] == contractor_id
        assert recall_res[0]["work_order_id"] == "WO-X01"
        assert recall_res[0]["rating"] == 5.0
        assert recall_res[0]["similarity"] > recall_res[1]["similarity"]

        # 2. Query with contractor filter - should return only Bob's job
        recall_res_filtered = await do_recall_similar_jobs(
            engine,
            {
                "namespace_id": ns_uuid,
                "query": "crossover filter or theater setup",
                "contractor_id": contractor_id,
            },
        )
        assert len(recall_res_filtered) == 1
        assert recall_res_filtered[0]["memory_id"] == str(mem1_id)
