"""Integration tests for Merkle chain verification and temporal decay pruning."""

from __future__ import annotations

import datetime
import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.config import cfg
from nce.cron import _decay_prune_tick, async_main
from nce.cron_lock import CronLock
from nce.db_utils import scoped_pg_session
from nce.event_log import append_event, verify_merkle_chain

# Ensure NCE_MASTER_KEY is populated for the config loader
os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chain_tamper_detection_integration(pg_pool, make_namespace, monkeypatch) -> None:
    """Verify that tampering with an event in the Merkle chain makes verify_merkle_chain return valid=False
    and reports the correct first_break sequence number.
    """
    ns_id = await make_namespace()
    agent_id = "test-chain-verify-agent"

    # Append 3 pristine events to namespace
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        for i in range(3):
            await append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id=agent_id,
                event_type="store_memory",
                params={
                    "saga_id": str(uuid.uuid4()),
                    "memory_id": str(uuid.uuid4()),
                    "payload_ref": f"00000000000000000000000{i}",
                    "assertion_type": "fact",
                    "entities": [],
                    "triplets": [],
                },
            )

    # Verify that the Merkle chain is initially valid
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        res_valid = await verify_merkle_chain(conn, namespace_id=ns_id)
        assert res_valid["valid"] is True
        assert res_valid["first_break"] is None

    # Tamper a row (event_seq = 2) in the event_log by bypassing WORM check
    monkeypatch.setenv("NCE_BYPASS_WORM", "true")
    monkeypatch.setattr(cfg, "NCE_BYPASS_WORM", True)

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute("ALTER TABLE event_log DISABLE TRIGGER trg_event_log_worm")
        try:
            await conn.execute(
                """
                UPDATE event_log
                SET params = '{"tampered": true, "saga_id": "00000000-0000-0000-0000-000000000000", "memory_id": "00000000-0000-0000-0000-000000000000", "payload_ref": "000000000000000000000000", "assertion_type": "fact", "entities": [], "triplets": []}'::jsonb
                WHERE namespace_id = $1 AND event_seq = 2
                """,
                ns_id,
            )
        finally:
            await conn.execute("ALTER TABLE event_log ENABLE TRIGGER trg_event_log_worm")

    # Verify that the Merkle chain is now invalid and the first break is sequence 2
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        res_invalid = await verify_merkle_chain(conn, namespace_id=ns_id)
        assert res_invalid["valid"] is False
        assert res_invalid["first_break"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_decay_job_scheduled(pg_pool, make_namespace, monkeypatch) -> None:
    """Verify that the decay prune job is registered with the correct ID on cron startup,
    and a run of the decay prune tick soft-deletes a faded memory row in the database.
    """

    # 1. Verify job ID is registered on scheduler boot
    class StopMain(Exception):
        pass

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()

    with (
        patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("asyncio.Event.wait", side_effect=StopMain),
        patch("nce.cron._renewal_tick", new_callable=AsyncMock),
        patch("nce.cron._reembedding_tick", new_callable=AsyncMock),
        patch("nce.cron._consolidation_tick", new_callable=AsyncMock),
        patch("nce.cron._partition_maintenance_tick", new_callable=AsyncMock),
        patch("nce.cron._saga_recovery_tick", new_callable=AsyncMock),
        patch("nce.cron._outbox_relay_tick", new_callable=AsyncMock),
        patch("nce.cron._decay_prune_tick", new_callable=AsyncMock),
        patch("nce.cron._chain_verification_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_sync_tick", new_callable=AsyncMock),
        patch("nce.cron._d365_netbox_bridge_tick", new_callable=AsyncMock),
    ):
        added_jobs = []

        def mock_add_job(func, trigger, *args, **kwargs):
            added_jobs.append(kwargs.get("id"))

        with patch("nce.cron.AsyncIOScheduler") as mock_scheduler_cls:
            mock_scheduler = MagicMock()
            mock_scheduler.add_job = mock_add_job
            mock_scheduler_cls.return_value = mock_scheduler

            try:
                await async_main()
            except StopMain:
                pass

            assert "phase_2_2_decay_prune" in added_jobs

    # 2. Verify a tick run soft-deletes a faded row
    ns_id = await make_namespace()

    faded_memory_id = uuid.uuid4()
    fresh_memory_id = uuid.uuid4()

    # 60 days ago is faded for 'episodic' (threshold ~56.91 days)
    faded_valid_from = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
    fresh_valid_from = datetime.datetime.now(datetime.timezone.utc)

    embedding = [0.1] * 768

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        # Insert faded memory
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, valid_from, created_at)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8::jsonb, $9, $10)
            """,
            faded_memory_id,
            ns_id,
            "test-agent",
            json.dumps(embedding),
            "fact",
            "episodic",
            "000000000000000000000001",
            "{}",
            faded_valid_from,
            datetime.datetime.now(datetime.timezone.utc),
        )

        # Insert fresh memory
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, valid_from, created_at)
            VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8::jsonb, $9, $10)
            """,
            fresh_memory_id,
            ns_id,
            "test-agent",
            json.dumps(embedding),
            "fact",
            "episodic",
            "000000000000000000000002",
            "{}",
            fresh_valid_from,
            datetime.datetime.now(datetime.timezone.utc),
        )

    # Mock acquire_cron_lock to bypass Redis and ensure it succeeds
    mock_lock = CronLock(
        job_id="decay_prune",
        key="local-disabled:decay_prune",
        token="local-disabled",
        ttl_seconds=3600,
    )

    with patch("nce.cron_lock.acquire_cron_lock", new_callable=AsyncMock, return_value=mock_lock):
        await _decay_prune_tick(pg_pool)

    # Verify the results in database
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        faded_row = await conn.fetchrow(
            "SELECT valid_to FROM memories WHERE id = $1", faded_memory_id
        )
        fresh_row = await conn.fetchrow(
            "SELECT valid_to FROM memories WHERE id = $1", fresh_memory_id
        )

        assert faded_row is not None
        assert faded_row["valid_to"] is not None  # Should be soft-deleted

        assert fresh_row is not None
        assert fresh_row["valid_to"] is None  # Should not be soft-deleted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_contradiction_atms_cascade(pg_pool, make_namespace) -> None:
    """Verify that resolving a contradiction with accepted_a soft-deletes the losing memory (B)
    and recursively invalidates consolidated memories that derive from it.
    """
    ns_id = await make_namespace()
    memory_a_id = uuid.uuid4()
    memory_b_id = uuid.uuid4()
    memory_c_id = uuid.uuid4()  # Consolidated memory derived from B
    contradiction_id = uuid.uuid4()
    embedding = [0.1] * 768

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        # 1. Insert memories A and B
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata)
            VALUES ($1, $2, 'test-agent', $3::vector, 'fact', 'episodic', '000000000000000000000001', '{}'::jsonb),
                   ($4, $2, 'test-agent', $3::vector, 'fact', 'episodic', '000000000000000000000002', '{}'::jsonb)
            """,
            memory_a_id,
            ns_id,
            json.dumps(embedding),
            memory_b_id,
        )

        # 2. Insert consolidated memory C derived from B
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, derived_from)
            VALUES ($1, $2, 'test-agent', $3::vector, 'fact', 'consolidated', '000000000000000000000003', '{}'::jsonb, $4::jsonb)
            """,
            memory_c_id,
            ns_id,
            json.dumps(embedding),
            json.dumps([str(memory_b_id)]),
        )

        # 3. Insert contradiction row
        await conn.execute(
            """
            INSERT INTO contradictions (id, namespace_id, memory_a_id, memory_b_id, agent_id, detection_path, signals, confidence, resolution)
            VALUES ($1, $2, $3, $4, 'system', 'sync', '{}'::jsonb, 0.9, NULL)
            """,
            contradiction_id,
            ns_id,
            memory_a_id,
            memory_b_id,
        )

    # 4. Resolve contradiction (accepted_a -> b is loser)
    from nce.orchestrators.cognitive import CognitiveOrchestrator

    orchestrator = CognitiveOrchestrator(pg_pool)
    await orchestrator.resolve_contradiction(
        contradiction_id=str(contradiction_id),
        namespace_id=str(ns_id),
        resolution="accepted_a",
        resolved_by="test-admin",
    )

    # 5. Verify results
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row_a = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_a_id)
        row_b = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_b_id)
        row_c = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_c_id)

        assert row_a is not None and row_a["valid_to"] is None
        assert row_b is not None and row_b["valid_to"] is not None
        assert row_c is not None and row_c["valid_to"] is not None

        # Verify event log has atms_cascade
        evt = await conn.fetchrow(
            "SELECT * FROM event_log WHERE namespace_id = $1 AND event_type = 'atms_cascade'",
            ns_id,
        )
        assert evt is not None
        evt_params = json.loads(evt["params"]) if isinstance(evt["params"], str) else evt["params"]
        assert evt_params["contradiction_id"] == str(contradiction_id)
        assert evt_params["invalidated_memory_id"] == str(memory_b_id)
        assert str(memory_c_id) in evt_params["invalidated_ids"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_contradiction_superseded_cascade(pg_pool, make_namespace) -> None:
    """Verify that resolving a contradiction with superseded soft-deletes the older memory."""
    ns_id = await make_namespace()
    memory_a_id = uuid.uuid4()
    memory_b_id = uuid.uuid4()
    contradiction_id = uuid.uuid4()
    embedding = [0.1] * 768

    now = datetime.datetime.now(datetime.timezone.utc)
    older_time = now - datetime.timedelta(hours=2)
    newer_time = now - datetime.timedelta(hours=1)

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        # Insert memory A (older)
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, created_at)
            VALUES ($1, $2, 'test-agent', $3::vector, 'fact', 'episodic', '000000000000000000000001', '{}'::jsonb, $4)
            """,
            memory_a_id,
            ns_id,
            json.dumps(embedding),
            older_time,
        )

        # Insert memory B (newer)
        await conn.execute(
            """
            INSERT INTO memories (id, namespace_id, agent_id, embedding, assertion_type, memory_type, payload_ref, metadata, created_at)
            VALUES ($1, $2, 'test-agent', $3::vector, 'fact', 'episodic', '000000000000000000000002', '{}'::jsonb, $4)
            """,
            memory_b_id,
            ns_id,
            json.dumps(embedding),
            newer_time,
        )

        # Insert contradiction
        await conn.execute(
            """
            INSERT INTO contradictions (id, namespace_id, memory_a_id, memory_b_id, agent_id, detection_path, signals, confidence, resolution)
            VALUES ($1, $2, $3, $4, 'system', 'sync', '{}'::jsonb, 0.9, NULL)
            """,
            contradiction_id,
            ns_id,
            memory_a_id,
            memory_b_id,
        )

    # Resolve with superseded (so older_id = memory_a_id is loser)
    from nce.orchestrators.cognitive import CognitiveOrchestrator

    orchestrator = CognitiveOrchestrator(pg_pool)
    await orchestrator.resolve_contradiction(
        contradiction_id=str(contradiction_id),
        namespace_id=str(ns_id),
        resolution="superseded",
        resolved_by="test-admin",
    )

    # Verify memory A is soft-deleted and memory B is still valid
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row_a = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_a_id)
        row_b = await conn.fetchrow("SELECT valid_to FROM memories WHERE id = $1", memory_b_id)

        assert row_a is not None and row_a["valid_to"] is not None
        assert row_b is not None and row_b["valid_to"] is None
