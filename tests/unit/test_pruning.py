"""
tests/unit/test_pruning.py
==========================
Unit tests for nce.database.pruning — Cascade Pruning Engine (BATCH-P2-003).

Call sequence for a full successful cascade_delete_tenant():
  execute calls (12 total):
    [0]  memories soft-delete
    [1]  event_log soft-delete
    [2]  v3_cognitive_ledger soft-delete
    [3]  topology_graph soft-delete
    [4]  memories.embedding zero-fill
    [5]  v3_cognitive_ledger.empathic_tensor zero-fill
    [6]  memories.value nullify
    [7]  memories.raw_pii_content nullify
    [8]  memories.raw_markdown nullify
    [9]  event_log.plaintext_secret nullify
    [10] event_log.raw_payload nullify
    [11] audit_log INSERT

  fetchval calls (2 total — consistency check):
    [0]  count of orphaned embeddings
    [1]  count of non-zero empathic tensors
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.database.pruning import (
    _ALLOWED_COLUMN_NAMES,
    _ALLOWED_TABLE_NAMES,
    _ALLOWED_ZERO_EXPRESSIONS,
    PruneResult,
    _DryRunRollback,
    _guard_column,
    _guard_table,
    _guard_zero_expr,
    batch_cascade_delete_tenants,
    cascade_delete_tenant,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

# Index constants — document the exact call-order contract
_IDX_SOFT_DELETE_MEMORIES = 0
_IDX_SOFT_DELETE_V3_LEDGER = 1
_IDX_SOFT_DELETE_TOPOLOGY = 2
_IDX_VECTOR_EMBEDDING = 3
_IDX_VECTOR_TENSOR = 4
_IDX_TEXT_VALUE = 5
_IDX_TEXT_PII = 6
_IDX_TEXT_MARKDOWN = 7
_IDX_TEXT_SECRET = 8
_IDX_TEXT_PAYLOAD = 9
_IDX_AUDIT_INSERT = 10
_TOTAL_EXECUTE_CALLS = 11


def _make_side_effect(
    *,
    soft_deletes: tuple[int, int, int] = (0, 0, 0),
    vectors: tuple[int, int] = (0, 0),
    texts: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0),
) -> list[str]:
    """Build a full 11-element execute side_effect list.

    Args:
        soft_deletes: Row counts for (memories, v3_cognitive_ledger, topology_graph).
        vectors:      Row counts for (memories.embedding, v3_cognitive_ledger.empathic_tensor).
        texts:        Row counts for (memories.value, memories.raw_pii_content,
                      memories.raw_markdown, event_log.plaintext_secret, event_log.raw_payload).
    """
    return (
        [f"UPDATE {n}" for n in soft_deletes]
        + [f"UPDATE {n}" for n in vectors]
        + [f"UPDATE {n}" for n in texts]
        + ["INSERT 0"]  # audit_log
    )


def _make_mock_pool_and_connection():
    """
    Factory: returns a (pool, conn) pair correctly mocked for asyncpg behaviour.

    Critical: asyncpg.Connection.transaction() is SYNCHRONOUS (returns a context
    manager, not a coroutine). The connection itself is AsyncMock so that
    execute() and fetchval() are awaitable, but transaction() must be a plain
    MagicMock returning the async context manager object.

    Defaults: all execute calls return "UPDATE 0"; all fetchval calls return 0
    (no orphaned vectors). Override per-test with side_effect / return_value.
    """
    conn = AsyncMock()

    # transaction() is sync in asyncpg — MagicMock, not AsyncMock
    class _MockTransaction:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return False  # do not suppress exceptions

    conn.transaction = MagicMock(return_value=_MockTransaction())

    # Defaults that handle any number of calls without StopIteration
    conn.execute = AsyncMock(return_value="UPDATE 0")
    conn.fetchval = AsyncMock(return_value=0)  # 0 = no orphaned vectors

    pool = MagicMock()

    class _MockAcquireContext:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    pool.acquire = MagicMock(return_value=_MockAcquireContext())
    pool.release = AsyncMock()
    return pool, conn


@pytest.fixture
def namespace_id() -> uuid.UUID:
    return uuid.UUID("12345678-1234-5678-1234-567812345678")


# ---------------------------------------------------------------------------
# 1. Allowlist guard tests (TD-PRUNE-3)
# ---------------------------------------------------------------------------


class TestAllowlistGuards:
    def test_allowed_table_names_are_non_empty(self):
        assert len(_ALLOWED_TABLE_NAMES) >= 4

    def test_allowed_column_names_include_vector_columns(self):
        assert "embedding" in _ALLOWED_COLUMN_NAMES
        assert "empathic_tensor" in _ALLOWED_COLUMN_NAMES

    def test_allowed_zero_expressions_include_null_and_vector(self):
        assert "NULL" in _ALLOWED_ZERO_EXPRESSIONS
        assert "'[0,0,0,0,0,0]'::vector" in _ALLOWED_ZERO_EXPRESSIONS

    def test_guard_table_passes_for_known_table(self):
        _guard_table("memories", "test")  # no exception

    def test_guard_table_raises_for_unknown_table(self):
        with pytest.raises(ValueError, match="Unsafe SQL table identifier"):
            _guard_table("pg_shadow", "test")

    def test_guard_column_passes_for_known_column(self):
        _guard_column("embedding", "test")  # no exception

    def test_guard_column_raises_for_unknown_column(self):
        with pytest.raises(ValueError, match="Unsafe SQL column identifier"):
            _guard_column("password", "test")

    def test_guard_zero_expr_passes_for_null(self):
        _guard_zero_expr("NULL")  # no exception

    def test_guard_zero_expr_raises_for_arbitrary_sql(self):
        with pytest.raises(ValueError, match="Unsafe SQL zero-expression"):
            _guard_zero_expr("(SELECT version())")


# ---------------------------------------------------------------------------
# 2. Dry-run sentinel tests (TD-PRUNE-1)
# ---------------------------------------------------------------------------


class TestDryRunSentinel:
    def test_dry_run_rollback_is_not_asyncpg_error(self):
        """_DryRunRollback must NOT be an asyncpg.PostgresError subclass."""
        import asyncpg

        assert not issubclass(_DryRunRollback, asyncpg.PostgresError)

    @pytest.mark.asyncio
    async def test_dry_run_returns_prune_result(self, namespace_id):
        """dry_run=True must return PruneResult without raising."""
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id, dry_run=True)
        assert isinstance(result, PruneResult)
        assert result.namespace_id == namespace_id

    @pytest.mark.asyncio
    async def test_dry_run_consistency_check_runs_before_rollback(self, namespace_id):
        """Consistency check must complete (pass) before the dry_run rollback fires."""
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id, dry_run=True)
        assert result.consistency_check_passed is True

    @pytest.mark.asyncio
    async def test_dry_run_captures_in_memory_counts(self, namespace_id):
        """In-memory row counts must be captured even though DB is rolled back."""
        pool, conn = _make_mock_pool_and_connection()
        conn.execute.side_effect = _make_side_effect(soft_deletes=(5, 4, 2))
        result = await cascade_delete_tenant(pool, namespace_id, dry_run=True)
        assert result.soft_deleted_rows == 11  # 5+4+2

    @pytest.mark.asyncio
    async def test_wet_run_does_not_raise(self, namespace_id):
        """dry_run=False (default) must complete without raising."""
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id, dry_run=False)
        assert isinstance(result, PruneResult)


# ---------------------------------------------------------------------------
# 3. Soft-deletion tests
# ---------------------------------------------------------------------------


class TestSoftDeletion:
    @pytest.mark.asyncio
    async def test_soft_delete_sql_sets_valid_to(self, namespace_id):
        """Phase 1 UPDATE must set valid_to = now()."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        first_call_sql = conn.execute.call_args_list[_IDX_SOFT_DELETE_MEMORIES][0][0]
        assert "valid_to = now()" in first_call_sql

    @pytest.mark.asyncio
    async def test_soft_delete_targets_only_non_deleted_rows(self, namespace_id):
        """Phase 1 WHERE clause must include valid_to IS NULL."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        first_call_sql = conn.execute.call_args_list[_IDX_SOFT_DELETE_MEMORIES][0][0]
        assert "valid_to IS NULL" in first_call_sql

    @pytest.mark.asyncio
    async def test_soft_delete_all_three_tables(self, namespace_id):
        """Phase 1 must UPDATE all three soft-delete tables."""
        pool, conn = _make_mock_pool_and_connection()
        conn.execute.side_effect = _make_side_effect(soft_deletes=(5, 4, 2))
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.soft_deleted_rows == 11

    @pytest.mark.asyncio
    async def test_soft_delete_counts_sum_correctly(self, namespace_id):
        """soft_deleted_rows in PruneResult is sum of all three UPDATE results."""
        pool, conn = _make_mock_pool_and_connection()
        conn.execute.side_effect = _make_side_effect(soft_deletes=(10, 0, 0))
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.soft_deleted_rows == 10


# ---------------------------------------------------------------------------
# 4. Vector zero-fill tests
# ---------------------------------------------------------------------------


class TestVectorZeroFilling:
    @pytest.mark.asyncio
    async def test_embedding_zero_filled_to_null(self, namespace_id):
        """memories.embedding must be SET to NULL (nullable column)."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        embedding_call_sql = conn.execute.call_args_list[_IDX_VECTOR_EMBEDDING][0][0]
        assert "embedding" in embedding_call_sql
        assert "NULL" in embedding_call_sql

    @pytest.mark.asyncio
    async def test_empathic_tensor_zero_filled_to_zero_vector(self, namespace_id):
        """v3_cognitive_ledger.empathic_tensor must be SET to [0,0,0,0,0,0]."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        tensor_call_sql = conn.execute.call_args_list[_IDX_VECTOR_TENSOR][0][0]
        assert "empathic_tensor" in tensor_call_sql
        assert "[0,0,0,0,0,0]" in tensor_call_sql

    @pytest.mark.asyncio
    async def test_vector_zero_fill_targets_soft_deleted_rows_only(self, namespace_id):
        """Phase 2 WHERE clause must include valid_to IS NOT NULL."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        embedding_sql = conn.execute.call_args_list[_IDX_VECTOR_EMBEDDING][0][0]
        assert "valid_to IS NOT NULL" in embedding_sql

    @pytest.mark.asyncio
    async def test_vectors_zeroed_count_is_sum_of_both_columns(self, namespace_id):
        """vectors_zeroed = memories.embedding count + v3_cognitive_ledger count."""
        pool, conn = _make_mock_pool_and_connection()
        conn.execute.side_effect = _make_side_effect(vectors=(3, 7))
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.vectors_zeroed == 10


# ---------------------------------------------------------------------------
# 5. Text nullification tests
# ---------------------------------------------------------------------------


class TestTextNullification:
    @pytest.mark.asyncio
    async def test_text_nullification_total_count(self, namespace_id):
        """text_columns_nullified is the sum of all 5 text column UPDATEs."""
        pool, conn = _make_mock_pool_and_connection()
        conn.execute.side_effect = _make_side_effect(texts=(2, 2, 2, 1, 1))
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.text_columns_nullified == 8

    @pytest.mark.asyncio
    async def test_memories_value_column_nullified(self, namespace_id):
        """memories.value must appear in a nullification UPDATE."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        value_sql = conn.execute.call_args_list[_IDX_TEXT_VALUE][0][0]
        assert "value" in value_sql
        assert "NULL" in value_sql

    @pytest.mark.asyncio
    async def test_event_log_plaintext_secret_nullified(self, namespace_id):
        """event_log.plaintext_secret must appear in a nullification UPDATE."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        secret_sql = conn.execute.call_args_list[_IDX_TEXT_SECRET][0][0]
        assert "plaintext_secret" in secret_sql
        assert "NULL" in secret_sql


# ---------------------------------------------------------------------------
# 6. Consistency check tests
# ---------------------------------------------------------------------------


class TestConsistencyCheck:
    @pytest.mark.asyncio
    async def test_consistency_passes_when_no_orphans(self, namespace_id):
        """When fetchval returns 0 for both checks, consistency_check_passed is True."""
        pool, conn = _make_mock_pool_and_connection()
        conn.fetchval.return_value = 0  # no orphans
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.consistency_check_passed is True

    @pytest.mark.asyncio
    async def test_consistency_fails_on_orphaned_embeddings(self, namespace_id):
        """Non-zero fetchval on first check raises ValueError and rolls back."""
        pool, conn = _make_mock_pool_and_connection()
        conn.fetchval.side_effect = [1, 0]  # embedding check finds orphan
        with pytest.raises(ValueError, match="Consistency check failed"):
            await cascade_delete_tenant(pool, namespace_id)

    @pytest.mark.asyncio
    async def test_consistency_fails_on_non_zero_tensors(self, namespace_id):
        """Non-zero fetchval on second check raises ValueError."""
        pool, conn = _make_mock_pool_and_connection()
        conn.fetchval.side_effect = [0, 2]  # tensor check finds non-zero
        with pytest.raises(ValueError, match="Consistency check failed"):
            await cascade_delete_tenant(pool, namespace_id)

    @pytest.mark.asyncio
    async def test_consistency_uses_fetchval_not_execute(self, namespace_id):
        """_check_orphaned_vectors must use fetchval, not execute (different asyncpg method)."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        # fetchval must have been called exactly twice (two consistency checks)
        assert conn.fetchval.call_count == 2
        # execute must have been called 12 times (4+2+5+1)
        assert conn.execute.call_count == _TOTAL_EXECUTE_CALLS

    @pytest.mark.asyncio
    async def test_consistency_sql_scopes_to_namespace(self, namespace_id):
        """Consistency check SQL must filter by namespace_id=$1."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        first_fetchval_sql = conn.fetchval.call_args_list[0][0][0]
        assert "namespace_id = $1" in first_fetchval_sql


# ---------------------------------------------------------------------------
# 7. Audit log tests
# ---------------------------------------------------------------------------


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_log_entry_uuid_is_set(self, namespace_id):
        """PruneResult.audit_log_entry_id must be a valid UUID."""
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id)
        assert isinstance(result.audit_log_entry_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_audit_log_insert_is_last_execute_call(self, namespace_id):
        """audit_log INSERT must be the 12th (final) execute call."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        last_sql = conn.execute.call_args_list[_IDX_AUDIT_INSERT][0][0]
        assert "INSERT INTO audit_log" in last_sql

    @pytest.mark.asyncio
    async def test_audit_log_uses_on_conflict_do_nothing(self, namespace_id):
        """INSERT must be idempotent via ON CONFLICT (id) DO NOTHING."""
        pool, conn = _make_mock_pool_and_connection()
        await cascade_delete_tenant(pool, namespace_id)
        audit_sql = conn.execute.call_args_list[_IDX_AUDIT_INSERT][0][0]
        assert "ON CONFLICT" in audit_sql
        assert "DO NOTHING" in audit_sql

    @pytest.mark.asyncio
    async def test_audit_log_receives_metadata_with_counts(self, namespace_id):
        """audit_log INSERT args must include a metadata dict with row counts."""
        pool, conn = _make_mock_pool_and_connection()
        conn.execute.side_effect = _make_side_effect(soft_deletes=(4, 0, 0))
        await cascade_delete_tenant(pool, namespace_id)
        audit_call_args = conn.execute.call_args_list[_IDX_AUDIT_INSERT][0]
        # arg[4] is the metadata JSONB dict
        metadata = audit_call_args[5]
        assert "soft_deleted_rows" in metadata
        assert metadata["soft_deleted_rows"] == 4


# ---------------------------------------------------------------------------
# 8. SLA compliance tests
# ---------------------------------------------------------------------------


class TestSLACompliance:
    @pytest.mark.asyncio
    async def test_sla_passes_for_fast_operation(self, namespace_id):
        """Mock operations complete in microseconds — SLA must pass."""
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.sla_passed is True
        assert result.duration_seconds < 5.0

    @pytest.mark.asyncio
    async def test_sla_field_always_present_on_prune_result(self, namespace_id):
        """PruneResult.sla_passed must be a bool regardless of speed."""
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id)
        assert isinstance(result.sla_passed, bool)

    @pytest.mark.asyncio
    async def test_duration_seconds_is_non_negative(self, namespace_id):
        """PruneResult.duration_seconds must always be >= 0."""
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# 9. Batch operation tests
# ---------------------------------------------------------------------------


class TestBatchOperations:
    @pytest.mark.asyncio
    async def test_batch_returns_one_result_per_namespace(self):
        """batch_cascade_delete_tenants returns len(namespace_ids) PruneResults."""
        pool, _ = _make_mock_pool_and_connection()
        namespaces = [uuid.uuid4() for _ in range(3)]
        results = await batch_cascade_delete_tenants(pool, namespaces)
        assert len(results) == 3
        assert all(isinstance(r, PruneResult) for r in results)

    @pytest.mark.asyncio
    async def test_batch_results_ordered_by_input(self):
        """Results must be in the same order as the input namespace_ids list."""
        pool, _ = _make_mock_pool_and_connection()
        namespaces = [uuid.uuid4() for _ in range(4)]
        results = await batch_cascade_delete_tenants(pool, namespaces)
        for i, ns_id in enumerate(namespaces):
            assert results[i].namespace_id == ns_id

    @pytest.mark.asyncio
    async def test_batch_all_sla_passed_for_mock_operations(self):
        """All mock operations complete in microseconds — all SLA flags True."""
        pool, _ = _make_mock_pool_and_connection()
        namespaces = [uuid.uuid4() for _ in range(5)]
        results = await batch_cascade_delete_tenants(pool, namespaces)
        assert all(r.sla_passed for r in results)


# ---------------------------------------------------------------------------
# 10. PruneResult type/invariant tests
# ---------------------------------------------------------------------------


class TestPruneResultInvariants:
    def test_is_namedtuple_with_required_fields(self, namespace_id):
        r = PruneResult(
            namespace_id=namespace_id,
            soft_deleted_rows=10,
            vectors_zeroed=5,
            text_columns_nullified=3,
            audit_log_entry_id=uuid.uuid4(),
            duration_seconds=2.5,
            consistency_check_passed=True,
            sla_passed=True,
        )
        assert isinstance(r, PruneResult)
        assert r.soft_deleted_rows == 10

    @pytest.mark.asyncio
    async def test_soft_deleted_rows_non_negative(self, namespace_id):
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.soft_deleted_rows >= 0

    @pytest.mark.asyncio
    async def test_vectors_zeroed_non_negative(self, namespace_id):
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.vectors_zeroed >= 0

    @pytest.mark.asyncio
    async def test_text_columns_nullified_non_negative(self, namespace_id):
        pool, _ = _make_mock_pool_and_connection()
        result = await cascade_delete_tenant(pool, namespace_id)
        assert result.text_columns_nullified >= 0
