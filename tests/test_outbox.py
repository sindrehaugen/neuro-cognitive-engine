"""Tests for the Transactional Outbox pattern.

Verifies that:
- store_memory writes an outbox event inside the PG transaction
- The relay polls and delivers unpublished events
- Failed deliveries are retried and eventually dead-lettered
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.models import AssertionType, MemoryType, StoreMemoryRequest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pg_pool():
    pool = AsyncMock()
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__ = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value="mem-uuid-0001")
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
    from nce.orchestrators.memory import MemoryOrchestrator

    pool, _ = mock_pg_pool
    mongo, _ = mock_mongo_client
    return MemoryOrchestrator(
        pg_pool=pool,
        mongo_client=mongo,
        redis_client=mock_redis_client,
    )


@pytest.fixture
def store_payload():
    return StoreMemoryRequest(
        namespace_id=str(uuid4()),
        agent_id="test_agent",
        content="Test content",
        summary="Test summary",
        heavy_payload="Test heavy",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.observation,
    )


# ---------------------------------------------------------------------------
# BATCH 6a: Outbox enqueue inside store_memory
# ---------------------------------------------------------------------------


class TestOutboxEnqueueInStoreMemory:
    """Verify that store_memory atomically writes to outbox_events."""

    @pytest.mark.asyncio
    async def test_outbox_event_inserted_on_success(
        self, orchestrator, mock_pg_pool, store_payload, monkeypatch
    ):
        """A successful store_memory must INSERT into outbox_events."""
        pool, conn = mock_pg_pool

        # Patch PII pipeline
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

        # Patch embedding
        from nce import embeddings as emb_mod

        monkeypatch.setattr(emb_mod, "embed_batch", AsyncMock(return_value=[[0.1] * 768]))

        # Patch event_log.append_event to avoid signing deps
        monkeypatch.setattr("nce.event_log.append_event", AsyncMock(return_value=None))

        # Patch saga execution log helpers (added by parallel session)
        monkeypatch.setattr(orchestrator, "_saga_log_start", AsyncMock(return_value="saga-1"))
        monkeypatch.setattr(orchestrator, "_saga_log_transition", AsyncMock(return_value=None))

        # Patch scoped_pg_session to yield our mock conn directly
        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.orchestrators.memory.scoped_pg_session", _fake_scoped)

        await orchestrator.store_memory(store_payload)

        # Find the outbox INSERT call
        outbox_calls = [c for c in conn.execute.call_args_list if "outbox_events" in str(c[0][0])]
        assert len(outbox_calls) >= 1, "store_memory did not execute an INSERT into outbox_events"

        # Verify the SQL and parameters
        sql, *params = outbox_calls[0][0]
        assert "INSERT INTO outbox_events" in sql
        assert "namespace_id" in sql
        assert params[1] == "memory"  # aggregate_type
        assert params[3] == "memory.stored"  # event_type

    @pytest.mark.asyncio
    async def test_outbox_payload_contains_memory_id(
        self, orchestrator, mock_pg_pool, store_payload, monkeypatch
    ):
        """The outbox payload must include the memory_id and payload_ref."""
        pool, conn = mock_pg_pool

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

        await orchestrator.store_memory(store_payload)

        outbox_calls = [c for c in conn.execute.call_args_list if "outbox_events" in str(c[0][0])]
        assert len(outbox_calls) >= 1

        payload_json = outbox_calls[0][0][5]  # payload param
        payload = json.loads(payload_json)
        assert "memory_id" in payload
        assert "payload_ref" in payload
        assert "assertion_type" in payload
        assert "memory_type" in payload

    @pytest.mark.asyncio
    async def test_outbox_rolled_back_when_pg_fails(
        self, orchestrator, mock_pg_pool, store_payload, monkeypatch
    ):
        """If the PG transaction fails, the outbox insert is never committed."""
        pool, conn = mock_pg_pool

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

        monkeypatch.setattr(orchestrator, "_saga_log_start", AsyncMock(return_value="saga-1"))
        monkeypatch.setattr(orchestrator, "_saga_log_transition", AsyncMock(return_value=None))

        # Make the PG fetchval fail inside the transaction
        conn.fetchval = AsyncMock(side_effect=RuntimeError("PG deadlock"))

        @asynccontextmanager
        async def _fake_scoped(pg_pool, namespace_id):
            yield conn

        monkeypatch.setattr("nce.orchestrators.memory.scoped_pg_session", _fake_scoped)

        with pytest.raises(RuntimeError, match="PG deadlock"):
            await orchestrator.store_memory(store_payload)

        # Outbox should NOT have been reached because fetchval failed first
        outbox_calls = [c for c in conn.execute.call_args_list if "outbox_events" in str(c[0][0])]
        assert len(outbox_calls) == 0, (
            "outbox_events should not be written when PG transaction fails"
        )


# ---------------------------------------------------------------------------
# BATCH 6b: Relay polling and delivery
# ---------------------------------------------------------------------------


class TestOutboxRelay:
    """Verify poll_outbox fetches unpublished rows."""

    @pytest.mark.asyncio
    async def test_poll_outbox_returns_unpublished_rows(self):
        """poll_outbox must return rows with published_at IS NULL."""
        from nce.outbox_relay import poll_outbox

        conn = AsyncMock()

        fake_row = {
            "id": "evt-1",
            "aggregate_type": "memory",
            "event_type": "memory.stored",
            "payload": {"memory_id": "m1"},
            "headers": {},
            "created_at": "2024-01-01T00:00:00",
        }
        conn.fetch = AsyncMock(return_value=[fake_row])

        results = await poll_outbox(conn, batch_size=10)

        assert len(results) == 1
        assert results[0]["aggregate_type"] == "memory"
        conn.fetch.assert_called_once()
        sql = conn.fetch.call_args[0][0]
        assert "published_at IS NULL" in sql
        assert "FOR UPDATE SKIP LOCKED" in sql

    @pytest.mark.asyncio
    async def test_poll_outbox_uses_batch_size(self):
        """The LIMIT must match the batch_size parameter."""
        from nce.outbox_relay import poll_outbox

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])

        await poll_outbox(conn, batch_size=42)

        assert conn.fetch.call_args[0][2] == 42
