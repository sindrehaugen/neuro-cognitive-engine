"""
tests/test_cognitive_orchestrator_rls.py

Tests for RLS enforcement in CognitiveOrchestrator:
- forget_memory must use scoped_session (not raw pg_pool.acquire)
- resolve_contradiction must use scoped_session + explicit namespace_id WHERE clause
- resolve_contradiction must log resolution via append_event
- Cross-tenant mutations must raise PermissionError
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_pool() -> MagicMock:
    """Returns a MagicMock that can be used as pg_pool."""
    return MagicMock()


@pytest.fixture
def scoped_conn() -> AsyncMock:
    """AsyncMock that supports ``async with conn:`` and ``async with conn.transaction():``."""
    # Inner transaction context manager
    tx = AsyncMock()
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = False

    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = False
    # transaction() returns a context manager, not a coroutine
    conn.transaction = MagicMock(return_value=tx)
    return conn


@pytest.fixture
def orchestrator(mock_pool: MagicMock, scoped_conn: AsyncMock, monkeypatch: Any) -> Any:
    """Build a CognitiveOrchestrator with scoped_session monkeypatched out.

    The real scoped_session is ``async def`` without ``@asynccontextmanager``,
    so ``self.scoped_session(ns)`` returns a coroutine.  We monkeypatch it to
    return an async context manager directly so the ``async with`` call sites work.
    """
    from contextlib import asynccontextmanager

    from nce.orchestrators.cognitive import CognitiveOrchestrator

    orch = CognitiveOrchestrator(pg_pool=mock_pool)

    @asynccontextmanager
    async def _patched_session(ns_id):
        yield scoped_conn

    orch.scoped_session = _patched_session  # type: ignore[method-assign]
    return orch


# ── forget_memory ───────────────────────────────────────────────────────────


class TestForgetMemoryRls:
    """Verify forget_memory enforces RLS via scoped_session."""

    @pytest.mark.asyncio
    async def test_uses_scoped_session_not_raw_pool(
        self, orchestrator: Any, mock_pool: MagicMock
    ) -> None:
        """forget_memory aquires via scoped_session, not pg_pool.acquire."""
        ns_id = str(uuid4())

        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            result = await orchestrator.forget_memory(
                memory_id=str(uuid4()),
                agent_id="test-agent",
                namespace_id=ns_id,
            )

        # pg_pool.acquire was NEVER called directly
        mock_pool.acquire.assert_not_called()
        assert result["status"] == "success"
        assert result["forgotten"] is True

    @pytest.mark.asyncio
    async def test_inserts_salience_zero_and_logs_event(
        self, orchestrator: Any, scoped_conn: AsyncMock
    ) -> None:
        """Salience is set to 0.0 and an event is logged."""
        ns_id = str(uuid4())
        mem_id = str(uuid4())

        with patch("nce.event_log.append_event", new_callable=AsyncMock) as mock_append:
            await orchestrator.forget_memory(
                memory_id=mem_id,
                agent_id="test-agent",
                namespace_id=ns_id,
            )

        # INSERT ... ON CONFLICT was executed
        scoped_conn.execute.assert_awaited()
        sql = scoped_conn.execute.call_args_list[0].args[0]
        assert "INSERT INTO memory_salience" in sql
        assert "0.0" in sql  # salience zeroed

        # Event was logged
        assert mock_append.await_count == 1
        kwargs = mock_append.call_args.kwargs
        assert kwargs["event_type"] == "forget_memory"
        assert kwargs["params"]["memory_id"] == mem_id

    @pytest.mark.asyncio
    async def test_wrapped_in_transaction(self, orchestrator: Any, scoped_conn: AsyncMock) -> None:
        """The INSERT and event log are inside a transaction."""
        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            await orchestrator.forget_memory(
                memory_id=str(uuid4()),
                agent_id="test-agent",
                namespace_id=str(uuid4()),
            )

        # The transaction context manager was entered and exited
        tx = scoped_conn.transaction.return_value
        tx.__aenter__.assert_called()
        tx.__aexit__.assert_called()


# ── resolve_contradiction ───────────────────────────────────────────────────


class TestResolveContradictionRls:
    """Verify resolve_contradiction enforces RLS + audit logging."""

    @pytest.mark.asyncio
    async def test_uses_scoped_session_not_raw_pool(
        self, orchestrator: Any, mock_pool: MagicMock, scoped_conn: AsyncMock
    ) -> None:
        """resolve_contradiction uses scoped_session, not pg_pool.acquire."""
        ns_id = str(uuid4())
        cont_id = str(uuid4())

        # fetchrow returns the updated row
        scoped_conn.fetchrow = AsyncMock(
            return_value={
                "id": cont_id,
                "namespace_id": ns_id,
                "resolution": "resolved_a",
                "resolved_by": "admin",
                "resolved_at": "2026-05-08T00:00:00Z",
            }
        )

        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            result = await orchestrator.resolve_contradiction(
                contradiction_id=cont_id,
                namespace_id=ns_id,
                resolution="resolved_a",
                resolved_by="admin",
            )

        # pg_pool.acquire was NEVER called directly
        mock_pool.acquire.assert_not_called()
        assert result["resolution"] == "resolved_a"

    @pytest.mark.asyncio
    async def test_update_includes_explicit_namespace_id_filter(
        self, orchestrator: Any, scoped_conn: AsyncMock
    ) -> None:
        """The UPDATE WHERE clause includes AND namespace_id = $2::uuid."""
        ns_id = str(uuid4())
        cont_id = str(uuid4())

        scoped_conn.fetchrow = AsyncMock(
            return_value={
                "id": cont_id,
                "namespace_id": ns_id,
                "resolution": "resolved_a",
                "resolved_by": "admin",
                "resolved_at": "2026-05-08T00:00:00Z",
            }
        )

        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            await orchestrator.resolve_contradiction(
                contradiction_id=cont_id,
                namespace_id=ns_id,
                resolution="resolved_a",
                resolved_by="admin",
            )

        sql = scoped_conn.fetchrow.call_args.args[0]
        assert "namespace_id = $2::uuid" in sql, (
            f"Expected explicit namespace_id filter in SQL, got: {sql}"
        )

    @pytest.mark.asyncio
    async def test_null_row_raises_permission_error(
        self, orchestrator: Any, scoped_conn: AsyncMock
    ) -> None:
        """If UPDATE returns 0 rows (cross-tenant), PermissionError is raised."""
        scoped_conn.fetchrow = AsyncMock(return_value=None)

        with (
            patch("nce.event_log.append_event", new_callable=AsyncMock),
            pytest.raises(PermissionError, match="not accessible in your namespace"),
        ):
            await orchestrator.resolve_contradiction(
                contradiction_id=str(uuid4()),
                namespace_id=str(uuid4()),
                resolution="resolved_a",
                resolved_by="attacker",
            )

    @pytest.mark.asyncio
    async def test_logs_resolution_event(self, orchestrator: Any, scoped_conn: AsyncMock) -> None:
        """Resolution is logged via cryptographically signed append_event."""
        ns_id = str(uuid4())
        cont_id = str(uuid4())

        scoped_conn.fetchrow = AsyncMock(
            return_value={
                "id": cont_id,
                "namespace_id": ns_id,
                "resolution": "resolved_a",
                "resolved_by": "admin",
                "resolved_at": "2026-05-08T00:00:00Z",
            }
        )

        with patch("nce.event_log.append_event", new_callable=AsyncMock) as mock_append:
            await orchestrator.resolve_contradiction(
                contradiction_id=cont_id,
                namespace_id=ns_id,
                resolution="resolved_a",
                resolved_by="admin",
                note="Testing resolution logging",
            )

        assert mock_append.await_count == 1
        kwargs = mock_append.call_args.kwargs
        assert kwargs["event_type"] == "resolve_contradiction"
        assert kwargs["namespace_id"] == UUID(ns_id)
        assert kwargs["agent_id"] == "admin"
        assert kwargs["params"]["contradiction_id"] == cont_id
        assert kwargs["params"]["resolution"] == "resolved_a"
        assert kwargs["params"]["note"] == "Testing resolution logging"
        assert kwargs["result_summary"] == {"status": "success"}

    @pytest.mark.asyncio
    async def test_wrapped_in_transaction(self, orchestrator: Any, scoped_conn: AsyncMock) -> None:
        """The UPDATE + event log are inside a transaction."""
        scoped_conn.fetchrow = AsyncMock(
            return_value={
                "id": str(uuid4()),
                "namespace_id": str(uuid4()),
                "resolution": "resolved_a",
                "resolved_by": "admin",
                "resolved_at": "2026-05-08T00:00:00Z",
            }
        )

        with patch("nce.event_log.append_event", new_callable=AsyncMock):
            await orchestrator.resolve_contradiction(
                contradiction_id=str(uuid4()),
                namespace_id=str(uuid4()),
                resolution="resolved_a",
                resolved_by="admin",
            )

        tx = scoped_conn.transaction.return_value
        tx.__aenter__.assert_called()
        tx.__aexit__.assert_called()
