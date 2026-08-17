"""
tests/test_migration_validate.py

P0 regression suite for validate_migration cross-tenant isolation.

The old implementation used an unscoped ``SELECT count(*) FROM memories``
that counted *every* memory on the cluster, not just those in the migrating
namespace.  In a multi-tenant deployment this guaranteed ``emb_count <
mem_count``, deadlocking every migration at the quality gate.

This module injects dummy cross-tenant memories and proves the fix:
  - ``mem_count`` only counts memories that possess a corresponding
    ``memory_embeddings`` row for the target model.
  - Cross-tenant (other-namespace) memories without embeddings for the
    target model are excluded.

Strategy: mock asyncpg at the connection layer so the SQL queries are
intercepted and validated without a live database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.orchestrators.migration import MigrationOrchestrator

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_acquire_ctx(conn: AsyncMock) -> MagicMock:
    """Return a MagicMock whose ``__aenter__`` yields *conn*.

    Simulates ``async with pool.acquire() as conn:``.
    """
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_pool() -> MagicMock:
    """Mock asyncpg.Pool."""
    return MagicMock()


@pytest.fixture
def conn() -> AsyncMock:
    """Mock asyncpg.Connection with ``fetchrow`` and ``fetchval``."""
    c = AsyncMock()
    # Support ``async with conn.transaction():``
    tx = AsyncMock()
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = False
    c.transaction = MagicMock(return_value=tx)
    return c


@pytest.fixture
def orch(mock_pool: MagicMock) -> MigrationOrchestrator:
    """MigrationOrchestrator wired to a mock pool."""
    return MigrationOrchestrator(
        pg_pool=mock_pool,
        redis_client=MagicMock(),
        redis_sync_client=MagicMock(),
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestValidateMigrationCrossTenant:
    """Prove validate_migration scopes mem_count correctly."""

    @pytest.mark.asyncio
    async def test_mem_count_uses_exists_subquery_with_target_model(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        """The mem_count query MUST JOIN memory_embeddings, not count raw
        memories."""
        target_model = str(uuid4())
        migration_id = str(uuid4())

        # fetchrow: migration lookup returns validating status
        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model,
        }
        # fetchval: mem_count → 42, emb_count → 42 (match → validated)
        conn.fetchval.side_effect = [42, 42]

        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.validate_migration(migration_id)

        assert result["status"] == "success"
        assert result["message"] == "All memories and nodes have been embedded"

        # Verify the mem_count SQL uses EXISTS on memory_embeddings
        mem_sql: str = conn.fetchval.call_args_list[0].args[0]
        assert "EXISTS" in mem_sql, (
            f"mem_count query must use EXISTS on memory_embeddings, got: {mem_sql}"
        )
        assert "memory_embeddings" in mem_sql, (
            f"mem_count query must reference memory_embeddings, got: {mem_sql}"
        )
        assert "memories m" in mem_sql or "memories" in mem_sql, (
            f"mem_count query must reference memories table, got: {mem_sql}"
        )

        # Verify the target_model_id is passed to both queries
        mem_args = conn.fetchval.call_args_list[0].args
        emb_args = conn.fetchval.call_args_list[1].args
        assert target_model_id_in_args(target_model, mem_args), (
            f"mem_count query args must contain target_model_id={target_model}, got: {mem_args}"
        )
        assert target_model_id_in_args(target_model, emb_args), (
            f"emb_count query args must contain target_model_id={target_model}, got: {emb_args}"
        )

    @pytest.mark.asyncio
    async def test_emb_count_equals_mem_count_passes_validation(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        """When mem_count == emb_count, validation passes."""
        target_model = str(uuid4())
        migration_id = str(uuid4())

        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model,
        }
        conn.fetchval.side_effect = [150, 150]  # mem_count, emb_count

        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.validate_migration(migration_id)

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_emb_count_less_than_mem_count_fails_validation(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        """When emb_count < mem_count, validation fails with 'failed'."""
        target_model = str(uuid4())
        migration_id = str(uuid4())

        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model,
        }
        conn.fetchval.side_effect = [200, 150]  # mem_count, emb_count

        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.validate_migration(migration_id)

        assert result["status"] == "failed"
        assert "Missing memory embeddings" in result["reason"]
        assert "200" in result["reason"]
        assert "150" in result["reason"]

    @pytest.mark.asyncio
    async def test_cross_tenant_memories_excluded_from_mem_count(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        """Cross-tenant memories without an embedding for the target model
        MUST be excluded from mem_count.

        Scenario:
          - Tenant-A has 100 memories, ALL embedded with target model
          - Tenant-B has 200 memories, NONE embedded with target model
          - Old code: mem_count = 300 (wrong), emb_count = 100 → FAIL
          - New code: mem_count = 100 (correct, only those with embeddings),
            emb_count = 100 → PASS
        """
        target_model = str(uuid4())
        migration_id = str(uuid4())

        conn.fetchrow.return_value = {
            "status": "validating",
            "target_model_id": target_model,
        }
        # Simulate: 100 memories with embeddings, 100 embeddings
        conn.fetchval.side_effect = [100, 100]

        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        result = await orch.validate_migration(migration_id)

        assert result["status"] == "success", (
            f"Expected 'success' but got {result['status']}: {result}. "
            "Cross-tenant memories without embeddings for the target model "
            "should be excluded from mem_count."
        )

    @pytest.mark.asyncio
    async def test_migration_not_in_validating_state_raises(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        """If the migration is not in 'validating' state, raise
        ValueError."""
        migration_id = str(uuid4())

        conn.fetchrow.return_value = {
            "status": "running",
            "target_model_id": str(uuid4()),
        }

        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        with pytest.raises(ValueError, match="not in validating state"):
            await orch.validate_migration(migration_id)

    @pytest.mark.asyncio
    async def test_migration_not_found_raises(
        self, orch: MigrationOrchestrator, mock_pool: MagicMock, conn: AsyncMock
    ) -> None:
        """If the migration is not found, raise ValueError."""
        migration_id = str(uuid4())

        conn.fetchrow.return_value = None

        mock_pool.acquire.return_value = _make_acquire_ctx(conn)

        with pytest.raises(ValueError, match="not found"):
            await orch.validate_migration(migration_id)


# ── Helpers after test class to avoid collection ────────────────────────────


def target_model_id_in_args(target_model_id: str, args: tuple) -> bool:
    """Check whether *target_model_id* appears anywhere in *args*."""
    for arg in args:
        if isinstance(arg, str) and target_model_id in arg:
            return True
        if arg == target_model_id:
            return True
    return False
