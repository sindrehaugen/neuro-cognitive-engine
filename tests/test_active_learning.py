from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.active_learning import ActiveLearningManager
from nce.config import cfg
from nce.models import AssertionType, MemoryType, StoreMemoryRequest
from nce.orchestrators.memory import MemoryOrchestrator

NS_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def mock_pg_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


@pytest.fixture
def mock_mongo_client():
    client = AsyncMock()
    db = AsyncMock()
    collection = AsyncMock()
    insert_result = MagicMock()
    insert_result.inserted_id = MagicMock()
    insert_result.inserted_id.__str__ = MagicMock(return_value="507f1f77bcf86cd799439011")
    collection.insert_one = AsyncMock(return_value=insert_result)
    collection.delete_one = AsyncMock()
    db.episodes = collection
    client.memory_archive = db
    return client, collection


@pytest.fixture
def mock_redis_client():
    redis = AsyncMock()
    redis.setex = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


@pytest.fixture
def orchestrator(mock_pg_pool, mock_mongo_client, mock_redis_client):
    pool, _ = mock_pg_pool
    mongo, _ = mock_mongo_client
    return MemoryOrchestrator(
        pg_pool=pool,
        mongo_client=mongo,
        redis_client=mock_redis_client,
    )


# ---------------------------------------------------------------------------
# Test Interception in store_memory
# ---------------------------------------------------------------------------


class TestActiveLearningInterception:
    @pytest.mark.asyncio
    async def test_store_memory_bypasses_quarantine_for_high_confidence(
        self, orchestrator, mock_pg_pool, monkeypatch
    ):
        """Memories with R >= 0.65 must bypass quarantine and save normally."""
        pool, conn = mock_pg_pool

        # High confidence payload (R = 0.8)
        payload = StoreMemoryRequest(
            namespace_id=NS_ID,
            agent_id="test_agent",
            content="Highly confident assertion.",
            summary="Confident summary",
            memory_type=MemoryType.episodic,
            assertion_type=AssertionType.fact,
            metadata={"confidence": 0.8},
        )

        # Mock PII and embeddings
        from nce.models import PIIProcessResult

        monkeypatch.setattr(
            orchestrator,
            "_apply_pii_pipeline",
            AsyncMock(
                return_value=(
                    PIIProcessResult(
                        sanitized_text="sanitized",
                        redacted=False,
                        entities_found=[],
                        vault_entries=[],
                    ),
                    "sanitized",
                    "sanitized heavy",
                    [],
                    [],
                )
            ),
        )
        from nce import embeddings as emb_mod

        monkeypatch.setattr(emb_mod, "embed_batch", AsyncMock(return_value=[[0.1] * 768]))
        monkeypatch.setattr("nce.event_log.append_event", AsyncMock(return_value=None))
        monkeypatch.setattr(orchestrator, "_saga_log_start", AsyncMock(return_value="saga-1"))
        monkeypatch.setattr(orchestrator, "_saga_log_transition", AsyncMock(return_value=None))

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.orchestrators.memory.scoped_pg_session", _fake_scoped)

        res = await orchestrator.store_memory(payload)

        assert res["quarantined"] is False
        assert "payload_ref" in res

        # Ensure active_learning_queue was not written to
        al_queue_calls = [
            c for c in conn.fetchval.call_args_list if "active_learning_queue" in str(c[0][0])
        ]
        assert len(al_queue_calls) == 0

    @pytest.mark.asyncio
    async def test_store_memory_quarantines_low_confidence(
        self, orchestrator, mock_pg_pool, monkeypatch
    ):
        """Memories with R < 0.65 must be quarantined."""
        pool, conn = mock_pg_pool

        # Low confidence payload (R = 0.5)
        payload = StoreMemoryRequest(
            namespace_id=NS_ID,
            agent_id="test_agent",
            content="Low confidence assertion.",
            summary="Low confidence summary",
            memory_type=MemoryType.episodic,
            assertion_type=AssertionType.fact,
            metadata={"R": 0.5},
        )

        fake_queue_id = uuid.uuid4()
        conn.fetchval = AsyncMock(return_value=fake_queue_id)

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.orchestrators.memory.scoped_pg_session", _fake_scoped)

        res = await orchestrator.store_memory(payload)

        assert res["quarantined"] is True
        assert res["queue_item_id"] == str(fake_queue_id)
        assert res["R"] == 0.5

        # Check that it inserted into the queue table
        insert_calls = [
            c for c in conn.fetchval.call_args_list if "active_learning_queue" in str(c[0][0])
        ]
        assert len(insert_calls) == 1
        assert "INSERT INTO active_learning_queue" in insert_calls[0][0][0]
        # Payload, R
        assert insert_calls[0][0][3] == payload.model_dump_json()
        assert insert_calls[0][0][4] == 0.5

    @pytest.mark.asyncio
    async def test_store_memory_parent_decay_check(self, orchestrator, mock_pg_pool, monkeypatch):
        """If R is not specified, evaluate Ebbinghaus parent memory decay."""
        pool, conn = mock_pg_pool

        parent_id = uuid.uuid4()
        payload = StoreMemoryRequest(
            namespace_id=NS_ID,
            agent_id="test_agent",
            content="Check parent decay.",
            derived_from=[parent_id],
        )

        # Mock parent salience score row: s_last = 0.8, updated 10 days ago.
        # With half_life = 10.0, decayed score should be 0.4.
        updated_at = datetime.now(timezone.utc)
        parent_row = {"salience_score": 0.8, "updated_at": updated_at}
        ns_row = {"metadata": json.dumps({"cognitive": {"half_life_days": 10.0}})}

        conn.fetchrow = AsyncMock(side_effect=[parent_row, ns_row])

        # Mock compute_decayed_score to return 0.4
        from nce import salience as sal_mod

        monkeypatch.setattr(sal_mod, "compute_decayed_score", MagicMock(return_value=0.4))

        fake_queue_id = uuid.uuid4()
        conn.fetchval = AsyncMock(return_value=fake_queue_id)

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.orchestrators.memory.scoped_pg_session", _fake_scoped)

        res = await orchestrator.store_memory(payload)

        assert res["quarantined"] is True
        assert res["R"] == 0.4
        assert res["queue_item_id"] == str(fake_queue_id)


# ---------------------------------------------------------------------------
# Test ActiveLearningManager Queue State Management & Gamification
# ---------------------------------------------------------------------------


class TestActiveLearningManager:
    @pytest.mark.asyncio
    async def test_confirm_memory_promotes_and_updates_queue(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool

        # Prepare stashed item
        original_req = StoreMemoryRequest(
            namespace_id=NS_ID,
            agent_id="test_agent",
            content="Quarantined text.",
        )
        fake_row = {
            "payload": original_req.model_dump_json(),
            "status": "pending",
            "agent_id": "test_agent",
        }
        conn.fetchrow = AsyncMock(return_value=fake_row)

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.active_learning.scoped_pg_session", _fake_scoped)

        # Patch append_event so confirm_memory's step-4 WORM emission does not
        # need a live DB / signing stack.
        import nce.event_log as real_el

        monkeypatch.setattr(real_el, "append_event", AsyncMock(return_value=None))

        al_mgr = ActiveLearningManager(pool)

        # Mock memory orchestrator
        mock_orch = AsyncMock()
        mock_orch.store_memory = AsyncMock(return_value={"payload_ref": "promoted-ref"})

        queue_item_id = uuid.uuid4()
        res = await al_mgr.confirm_memory(NS_ID, queue_item_id, "operator-123", mock_orch)

        assert res["payload_ref"] == "promoted-ref"

        # Verify orchestrator store_memory was called with bypass flag
        mock_orch.store_memory.assert_called_once()
        passed_req = mock_orch.store_memory.call_args[0][0]
        assert passed_req.metadata["bypass_quarantine"] is True

        # Verify database update to 'confirmed'
        update_calls = [
            c for c in conn.execute.call_args_list if "UPDATE active_learning_queue" in str(c[0][0])
        ]
        assert len(update_calls) == 1
        assert "confirmed" in update_calls[0][0][0]
        assert update_calls[0][0][1] == "operator-123"

    @pytest.mark.asyncio
    async def test_reject_memory_updates_status(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool

        conn.fetchrow = AsyncMock(
            return_value={
                "status": "pending",
                "payload": '{"content": "stub"}',
                "agent_id": "test_agent",
            }
        )
        # Patch append_event so unit test does not need a live DB/signing stack.
        import nce.event_log as real_el

        monkeypatch.setattr(real_el, "append_event", AsyncMock(return_value=None))

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.active_learning.scoped_pg_session", _fake_scoped)

        al_mgr = ActiveLearningManager(pool)
        queue_item_id = uuid.uuid4()
        await al_mgr.reject_memory(NS_ID, queue_item_id, "operator-123")

        update_calls = [
            c for c in conn.execute.call_args_list if "UPDATE active_learning_queue" in str(c[0][0])
        ]
        assert len(update_calls) == 1
        assert "rejected" in update_calls[0][0][0]
        assert update_calls[0][0][1] == "operator-123"

    @pytest.mark.asyncio
    async def test_get_gamified_stats_computes_correctly(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool

        # Mock various count values returned from DB queries
        # Order of fetchval: pending_count, confirmed_count, rejected_count, op_confirmed, op_rejected
        conn.fetchval = AsyncMock(
            side_effect=[
                10,  # pending_count
                30,  # confirmed_count
                10,  # rejected_count
                20,  # op_confirmed
                5,  # op_rejected
            ]
        )

        # Mock streaks resolved rows
        streak_rows = [
            {"status": "confirmed", "resolved_by": "operator-123"},
            {"status": "rejected", "resolved_by": "operator-123"},
            {"status": "confirmed", "resolved_by": "operator-123"},
            {"status": "confirmed", "resolved_by": "someone-else"},
        ]
        conn.fetch = AsyncMock(return_value=streak_rows)

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.active_learning.scoped_pg_session", _fake_scoped)

        al_mgr = ActiveLearningManager(pool)
        stats = await al_mgr.get_gamified_stats(NS_ID, "operator-123")

        assert stats["pending_count"] == 10
        assert stats["confirmed_count"] == 30
        assert stats["rejected_count"] == 10
        assert stats["accuracy_rate"] == 0.75  # 30 / (30 + 10)

        op_stats = stats["operator_stats"]
        assert op_stats["operator_id"] == "operator-123"
        assert op_stats["confirmed_count"] == 20
        assert op_stats["rejected_count"] == 5
        # XP = 20 * 10 + 5 * 5 = 225
        assert op_stats["xp"] == 225
        assert op_stats["level"] == 3  # 1 + (225 // 100)
        assert op_stats["xp_to_next_level"] == 75  # 100 - 25
        assert op_stats["streak"] == 3  # Streak broken at index 3 by someone-else

    @pytest.mark.asyncio
    async def test_get_gamified_stats_custom_xp_config(self, mock_pg_pool, monkeypatch):
        pool, conn = mock_pg_pool
        # Override default XP values
        monkeypatch.setattr(cfg, "NCE_ACTIVE_LEARNING_CONFIRM_XP", 50)
        monkeypatch.setattr(cfg, "NCE_ACTIVE_LEARNING_REJECT_XP", 20)

        conn.fetchval = AsyncMock(
            side_effect=[
                0,  # pending
                0,  # confirmed
                0,  # rejected
                10,  # op_confirmed
                5,  # op_rejected
            ]
        )
        conn.fetch = AsyncMock(return_value=[])

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.active_learning.scoped_pg_session", _fake_scoped)

        al_mgr = ActiveLearningManager(pool)
        stats = await al_mgr.get_gamified_stats(NS_ID, "operator-123")
        op_stats = stats["operator_stats"]
        # XP = 10 * 50 + 5 * 20 = 600
        assert op_stats["xp"] == 600
        assert op_stats["level"] == 7
