from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import nce.outbox_relay as outbox_relay
import nce.tasks as tasks
from nce.orchestrator import NCEEngine


class DummyLock:
    """A dummy lock that does not bind to any specific event loop."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def test_local_nce_engine_initialization():
    """Verify that NCEEngine is initialized and connected locally for each call

    to process_code_indexing, and mock Redis and DB connection pools
    to avoid actual connection attempts.
    """
    # Save original tasks module states to prevent test pollution
    orig_redis_client = tasks._redis_client

    tasks._redis_client = None

    try:
        mock_redis = MagicMock()
        mock_redis.delete = MagicMock()
        mock_redis.incr.return_value = 1

        # Mock embedding and parsing functions
        mock_embed_batch = AsyncMock(return_value=[[0.1] * 1536])
        mock_chunk = MagicMock()
        mock_chunk.name = "mock_chunk"
        mock_chunk.code_string = "def foo(): pass"
        mock_chunk.node_type = "function"
        mock_chunk.start_line = 1
        mock_chunk.end_line = 2
        mock_parse_file = MagicMock(return_value=[mock_chunk])

        # Mock PG connection and transaction
        @asynccontextmanager
        async def mock_transaction_cm():
            yield

        mock_conn = MagicMock()
        mock_conn.transaction = MagicMock(side_effect=mock_transaction_cm)
        mock_conn.execute = AsyncMock()
        mock_conn.fetch = AsyncMock()

        @asynccontextmanager
        async def mock_unmanaged_conn(pool, site):
            yield mock_conn

        @asynccontextmanager
        async def mock_scoped_session(self_inst, namespace_id):
            yield mock_conn

        # Mock the engine connect/disconnect methods
        async def mock_connect(self_inst):
            self_inst.mongo_client = MagicMock()
            # Setup mongo insert_one return value
            mock_insert_result = MagicMock()
            mock_insert_result.inserted_id = "mock_mongo_id"
            self_inst.mongo_client.memory_archive.code_files.insert_one = AsyncMock(
                return_value=mock_insert_result
            )
            self_inst.mongo_client.memory_archive.code_files.delete_one = AsyncMock()
            self_inst.pg_pool = MagicMock()
            self_inst.redis_client = MagicMock()
            self_inst.redis_client.setex = AsyncMock()

        async def mock_disconnect(self_inst):
            pass

        with (
            patch.object(
                NCEEngine, "connect", autospec=True, side_effect=mock_connect
            ) as mock_connect_spy,
            patch.object(
                NCEEngine, "disconnect", autospec=True, side_effect=mock_disconnect
            ) as mock_disconnect_spy,
            patch("nce.tasks.Redis") as mock_redis_cls,
            patch("nce.tasks.parse_file", new=mock_parse_file),
            patch("nce.tasks._embeddings.embed_batch", new=mock_embed_batch),
            patch("nce.tasks.unmanaged_pg_connection", new=mock_unmanaged_conn),
            patch.object(NCEEngine, "scoped_session", new=mock_scoped_session),
        ):
            mock_redis_cls.from_url.return_value = mock_redis

            # Call process_code_indexing multiple times (once with namespace, once without)
            ns_id = str(uuid4())
            result1 = tasks.process_code_indexing(
                filepath="test.py",
                raw_code="def foo(): pass",
                language="python",
                user_id="user_123",
                namespace_id=ns_id,
            )
            result2 = tasks.process_code_indexing(
                filepath="test.py", raw_code="def foo(): pass", language="python"
            )

            # Assert results
            assert result1 == {"status": "success", "chunks": 1}
            assert result2 == {"status": "success", "chunks": 1}

            # Assert that NCEEngine.connect was called twice (local initialization per task execution)
            assert mock_connect_spy.call_count == 2
            # Assert that NCEEngine.disconnect was called twice
            assert mock_disconnect_spy.call_count == 2
    finally:
        # Restore module state
        tasks._redis_client = orig_redis_client


@pytest.mark.asyncio
async def test_structural_outbox_failure_dlq_bypass():
    """Verify that when run_outbox_relay_once encounters OutboxDeliveryError

    (raised on unknown/unregistered outbox schema/event type), it immediately
    sets attempt_count = MAX_OUTBOX_ATTEMPTS and inserts it straight to the
    dead_letter_queue without running retry loops.
    """
    mock_pool = MagicMock()

    @asynccontextmanager
    async def mock_transaction_cm():
        yield

    mock_conn = MagicMock()
    mock_conn.transaction = MagicMock(side_effect=mock_transaction_cm)
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock()

    # Mock pool.acquire context manager
    @asynccontextmanager
    async def mock_acquire(timeout):
        yield mock_conn

    mock_pool.acquire.side_effect = mock_acquire

    # Prepare an unregistered/unknown event
    event_id = uuid4()
    namespace_id = uuid4()
    aggregate_id = uuid4()
    event_payload = {"test_key": "test_val"}

    mock_event = {
        "id": event_id,
        "namespace_id": namespace_id,
        "aggregate_type": "test_aggregate",
        "aggregate_id": aggregate_id,
        "event_type": "unregistered.event",
        "payload": json.dumps(event_payload),
        "headers": None,
        "attempt_count": 0,
        "created_at": datetime.now(timezone.utc),
    }

    # poll_outbox calls conn.fetch, so return the mock_event inside a list
    mock_conn.fetch.return_value = [mock_event]

    # Run the outbox relay
    delivered = await outbox_relay.run_outbox_relay_once(mock_pool)

    # 1. Assert that the returned delivered count is 0 (delivery failed)
    assert delivered == 0

    # 2. Verify conn.execute was called to update outbox_events to MAX_OUTBOX_ATTEMPTS (5) immediately
    # We look at the calls to mock_conn.execute.
    update_call = None
    insert_call = None

    for call in mock_conn.execute.call_args_list:
        query = call[0][0]
        args = call[0][1:]
        if "UPDATE outbox_events" in query:
            update_call = (query, args)
        elif "INSERT INTO dead_letter_queue" in query:
            insert_call = (query, args)

    assert update_call is not None, "Expected UPDATE outbox_events query to be executed"
    assert insert_call is not None, "Expected INSERT INTO dead_letter_queue query to be executed"

    # Verify update arguments: (MAX_OUTBOX_ATTEMPTS, error_message, event_id)
    update_args = update_call[1]
    assert update_args[0] == outbox_relay.MAX_OUTBOX_ATTEMPTS  # 5
    assert "OutboxDeliveryError" in update_args[1]
    assert update_args[2] == event_id

    # Verify insert arguments:
    # (namespace_id, task_name, job_id, kwargs_json, error_message, attempt_count, 'pending')
    insert_args = insert_call[1]
    assert insert_args[0] == namespace_id
    assert insert_args[1] == "outbox:unregistered.event"
    assert insert_args[2] == str(event_id)

    # kwargs_json should contain the outbox event details
    kwargs_data = json.loads(insert_args[3])
    assert kwargs_data["outbox_event_id"] == str(event_id)
    assert kwargs_data["event_type"] == "unregistered.event"
    assert kwargs_data["aggregate_type"] == "test_aggregate"
    assert kwargs_data["aggregate_id"] == str(aggregate_id)
    assert kwargs_data["payload"] == event_payload

    assert "OutboxDeliveryError" in insert_args[4]
    assert insert_args[5] == outbox_relay.MAX_OUTBOX_ATTEMPTS  # 5 (attempt_count)


def test_engine_is_not_reused_across_loop_boundaries():
    """Verify that NCEEngine is not reused across event loop boundaries.

    Each call to process_code_indexing must instantiate a fresh NCEEngine
    and run its connect lifecycle independently.
    """
    orig_redis_client = tasks._redis_client
    tasks._redis_client = None

    try:
        mock_redis = MagicMock()
        mock_redis.delete = MagicMock()
        mock_redis.incr.return_value = 1

        mock_embed_batch = AsyncMock(return_value=[[0.1] * 1536])
        mock_chunk = MagicMock()
        mock_chunk.name = "mock_chunk"
        mock_chunk.code_string = "def foo(): pass"
        mock_chunk.node_type = "function"
        mock_chunk.start_line = 1
        mock_chunk.end_line = 2
        mock_parse_file = MagicMock(return_value=[mock_chunk])

        @asynccontextmanager
        async def mock_transaction_cm():
            yield

        mock_conn = MagicMock()
        mock_conn.transaction = MagicMock(side_effect=mock_transaction_cm)
        mock_conn.execute = AsyncMock()
        mock_conn.fetch = AsyncMock()

        @asynccontextmanager
        async def mock_unmanaged_conn(pool, site):
            yield mock_conn

        @asynccontextmanager
        async def mock_scoped_session(self_inst, namespace_id):
            yield mock_conn

        async def mock_connect(self_inst):
            self_inst.mongo_client = MagicMock()
            mock_insert_result = MagicMock()
            mock_insert_result.inserted_id = "mock_mongo_id"
            self_inst.mongo_client.memory_archive.code_files.insert_one = AsyncMock(
                return_value=mock_insert_result
            )
            self_inst.mongo_client.memory_archive.code_files.delete_one = AsyncMock()
            self_inst.pg_pool = MagicMock()
            self_inst.redis_client = MagicMock()
            self_inst.redis_client.setex = AsyncMock()

        async def mock_disconnect(self_inst):
            pass

        with (
            patch.object(
                NCEEngine, "connect", autospec=True, side_effect=mock_connect
            ) as mock_connect_spy,
            patch.object(
                NCEEngine, "disconnect", autospec=True, side_effect=mock_disconnect
            ) as mock_disconnect_spy,
            patch("nce.tasks.Redis") as mock_redis_cls,
            patch("nce.tasks.parse_file", new=mock_parse_file),
            patch("nce.tasks._embeddings.embed_batch", new=mock_embed_batch),
            patch("nce.tasks.unmanaged_pg_connection", new=mock_unmanaged_conn),
            patch.object(NCEEngine, "scoped_session", new=mock_scoped_session),
        ):
            mock_redis_cls.from_url.return_value = mock_redis

            # Execute two consecutive calls to tasks.process_code_indexing()
            result1 = tasks.process_code_indexing(
                filepath="test.py",
                raw_code="def foo(): pass",
                language="python",
                user_id="user_123",
                namespace_id=str(uuid4()),
            )
            result2 = tasks.process_code_indexing(
                filepath="test.py",
                raw_code="def foo(): pass",
                language="python",
                user_id="user_123",
                namespace_id=str(uuid4()),
            )

            # Assert results are successful
            assert result1 == {"status": "success", "chunks": 1}
            assert result2 == {"status": "success", "chunks": 1}

            # Assert that NCEEngine.connect was called exactly twice
            assert mock_connect_spy.call_count == 2
            # Assert that NCEEngine.disconnect was called exactly twice
            assert mock_disconnect_spy.call_count == 2

    finally:
        tasks._redis_client = orig_redis_client
