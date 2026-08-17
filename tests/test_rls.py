"""
Tests for Row Level Security (RLS) connection lifecycles, transaction scopes, and audited sessions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from nce.auth import audited_session
from nce.db_utils import scoped_pg_session
from tests.fixtures.fake_asyncpg import RecordingFakeConnection, RecordingFakePool


class MockRLSConnection(RecordingFakeConnection):
    """Subclass of RecordingFakeConnection that records set_config execute calls."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.recorded_queries: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.recorded_queries.append((query, args))
        if "set_config" in query:
            return "SELECT 1"
        if "pg_advisory_xact_lock" in query:
            return "SELECT 1"
        return await super().execute(query, *args)


@pytest.fixture
def fake_pool() -> tuple[RecordingFakePool, MockRLSConnection]:
    conn = MockRLSConnection()
    pool = RecordingFakePool(conn)
    return pool, conn


@pytest.mark.asyncio
async def test_scoped_pg_session_success(
    fake_pool: tuple[RecordingFakePool, MockRLSConnection],
) -> None:
    """Test that scoped_pg_session checks out a connection, wraps in transaction, and sets context."""
    pool, conn = fake_pool
    namespace_id = uuid4()

    async with scoped_pg_session(pool, namespace_id) as yielded_conn:
        assert yielded_conn is conn
        # Should be inside transaction
        assert conn.is_in_transaction()

        # Context namespace must be set
        set_config_calls = [q for q in conn.recorded_queries if "set_config" in q[0]]
        assert len(set_config_calls) == 1
        assert set_config_calls[0][1][0] == str(namespace_id)

    # After session exits, transaction should be closed (depth == 0)
    assert not conn.is_in_transaction()

    # SET LOCAL is cleared at transaction end — no explicit reset query (FIX-011).
    set_config_calls = [q for q in conn.recorded_queries if "set_config" in q[0]]
    assert len(set_config_calls) == 1
    assert set_config_calls[0][1][0] == str(namespace_id)


@pytest.mark.asyncio
async def test_scoped_pg_session_exception_resets_context(
    fake_pool: tuple[RecordingFakePool, MockRLSConnection],
) -> None:
    """Test that context is still reset and transaction closed even if block raises an exception."""
    pool, conn = fake_pool
    namespace_id = uuid4()

    with pytest.raises(ValueError, match="Block error"):
        async with scoped_pg_session(pool, namespace_id):
            assert conn.is_in_transaction()
            raise ValueError("Block error")

    # Transaction closed (SET LOCAL cleared on rollback)
    assert not conn.is_in_transaction()

    set_config_calls = [q for q in conn.recorded_queries if "set_config" in q[0]]
    assert len(set_config_calls) == 1


@pytest.mark.asyncio
async def test_scoped_pg_session_requires_namespace(
    fake_pool: tuple[RecordingFakePool, MockRLSConnection],
) -> None:
    """Test that scoped_pg_session fails closed if namespace is missing/invalid."""
    pool, conn = fake_pool

    with pytest.raises(ValueError, match="namespace_id is required"):
        async with scoped_pg_session(pool, None):  # type: ignore
            pass

    with pytest.raises(ValueError, match="namespace_id is required"):
        async with scoped_pg_session(pool, ""):
            pass


@pytest.mark.asyncio
async def test_audited_session_flow(fake_pool: tuple[RecordingFakePool, MockRLSConnection]) -> None:
    """Test audited_session flow: audit event written, connection RLS scoped."""
    pool, conn = fake_pool
    namespace_id = uuid4()

    # Mock the central append_event to avoid requiring cryptographic schemas
    with patch("nce.event_log.append_event", new_callable=AsyncMock) as mock_append:
        async with audited_session(
            pg_pool=pool,
            namespace_id=namespace_id,
            agent_id="test-agent",
            event_type="test_event",
            params={"meta": "data"},
            reason="just testing",
        ) as yielded_conn:
            assert yielded_conn is conn
            assert conn.is_in_transaction()

            # Context set
            set_config_calls = [q for q in conn.recorded_queries if "set_config" in q[0]]
            assert len(set_config_calls) == 1
            assert set_config_calls[0][1][0] == str(namespace_id)

            # Audit write must have been called
            assert mock_append.called
            # The first argument is conn, second is namespace_id
            assert mock_append.call_args[1]["namespace_id"] == namespace_id
            assert mock_append.call_args[1]["agent_id"] == "test-agent"
            assert mock_append.call_args[1]["event_type"] == "test_event"
            assert mock_append.call_args[1]["params"]["reason"] == "just testing"

        assert not conn.is_in_transaction()
        set_config_calls = [q for q in conn.recorded_queries if "set_config" in q[0]]
        assert len(set_config_calls) == 2


@pytest.mark.asyncio
async def test_audited_session_fails_closed_on_audit_failure(
    fake_pool: tuple[RecordingFakePool, MockRLSConnection],
) -> None:
    """Test that if audit log write fails, audited_session immediately raises and block never executes."""
    pool, conn = fake_pool
    namespace_id = uuid4()
    block_executed = False

    with patch("nce.event_log.append_event", side_effect=RuntimeError("DB write error")):
        with pytest.raises(RuntimeError, match="Audit write failed"):
            async with audited_session(
                pg_pool=pool,
                namespace_id=namespace_id,
                agent_id="test-agent",
                event_type="test_event",
            ):
                block_executed = True

    assert not block_executed
    # Context never set on connection
    assert len(conn.recorded_queries) == 0
