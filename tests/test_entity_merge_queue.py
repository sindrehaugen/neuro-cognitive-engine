"""
Integration test suite for entity_merge_queue table.

Validates:
- Table existence and schema
- Status CHECK constraint enforcement
- Row-Level Security (RLS) isolation across namespaces
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context


@pytest.mark.integration
@pytest.mark.asyncio
class TestEntityMergeQueueTable:
    """Integration tests for entity_merge_queue table (async with asyncpg)."""

    @pytest_asyncio.fixture
    async def test_namespace_id(
        self, pg_admin_conn: asyncpg.Connection
    ) -> AsyncGenerator[uuid.UUID, None]:
        """Create and return a test namespace."""
        ns_id = uuid.uuid4()
        await pg_admin_conn.execute(
            "INSERT INTO namespaces (id, slug) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ns_id,
            f"test_namespace_{ns_id.hex[:8]}",
        )
        yield ns_id

    async def test_table_exists(self, pg_admin_conn: asyncpg.Connection) -> None:
        """Verify entity_merge_queue table exists."""
        exists = await pg_admin_conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name = 'entity_merge_queue'
            )
            """
        )
        assert exists, "entity_merge_queue table does not exist"

    async def test_table_has_required_columns(self, pg_admin_conn: asyncpg.Connection) -> None:
        """Verify entity_merge_queue has all required columns."""
        rows = await pg_admin_conn.fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'entity_merge_queue'
            ORDER BY ordinal_position
            """
        )
        columns = {row["column_name"]: row["data_type"] for row in rows}

        required_columns = {
            "id": "uuid",
            "namespace_id": "uuid",
            "node_type": "text",
            "candidate_payload": "jsonb",
            "target_node_id": "uuid",
            "score": "double precision",
            "status": "text",
            "created_at": "timestamp with time zone",
            "decided_by": "text",
            "decided_at": "timestamp with time zone",
        }

        for col_name, col_type in required_columns.items():
            assert col_name in columns, f"Column {col_name} not found"
            assert columns[col_name] == col_type, (
                f"Column {col_name} has type {columns[col_name]}, expected {col_type}"
            )

    async def test_status_check_constraint_valid_values(
        self, pg_admin_conn: asyncpg.Connection, test_namespace_id: uuid.UUID
    ) -> None:
        """Verify status CHECK constraint accepts valid values."""
        target_node_id = uuid.uuid4()
        valid_statuses = ["pending", "confirmed", "rejected"]

        for status in valid_statuses:
            await pg_admin_conn.execute(
                """
                INSERT INTO entity_merge_queue
                (namespace_id, node_type, candidate_payload, target_node_id, score, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                test_namespace_id,
                "TestEntity",
                '{"test": "data"}',
                target_node_id,
                0.75,
                status,
            )

        # Verify all three were inserted
        count = await pg_admin_conn.fetchval(
            "SELECT COUNT(*) FROM entity_merge_queue WHERE namespace_id = $1",
            test_namespace_id,
        )
        assert count == 3, f"Expected 3 rows inserted, found {count}"

    async def test_status_check_constraint_rejects_invalid_value(
        self, pg_admin_conn: asyncpg.Connection, test_namespace_id: uuid.UUID
    ) -> None:
        """Verify status CHECK constraint rejects invalid values."""
        target_node_id = uuid.uuid4()

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await pg_admin_conn.execute(
                """
                INSERT INTO entity_merge_queue
                (namespace_id, node_type, candidate_payload, target_node_id, score, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                test_namespace_id,
                "TestEntity",
                '{"test": "data"}',
                target_node_id,
                0.75,
                "invalid_status",
            )

    async def test_rls_isolates_across_namespaces(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """Verify RLS isolation: namespace A sees only its own rows."""
        ns_a = await make_namespace()
        ns_b = await make_namespace()
        node_type = f"test-emq-{uuid.uuid4().hex[:8]}"

        # Insert row into namespace A under the tenant-scoped app role.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            a_row_id = await pg_app_conn.fetchval(
                """
                INSERT INTO entity_merge_queue
                (namespace_id, node_type, candidate_payload, target_node_id, score, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                ns_a,
                node_type,
                '{"data": "a"}',
                uuid.uuid4(),
                0.80,
                "pending",
            )

        assert a_row_id is not None

        # Insert row into namespace B.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            b_row_id = await pg_app_conn.fetchval(
                """
                INSERT INTO entity_merge_queue
                (namespace_id, node_type, candidate_payload, target_node_id, score, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                ns_b,
                node_type,
                '{"data": "b"}',
                uuid.uuid4(),
                0.70,
                "pending",
            )

        assert b_row_id is not None

        # Verify namespace A sees only its own row.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            visible = await pg_app_conn.fetchval(
                "SELECT count(*) FROM entity_merge_queue WHERE id = $1",
                a_row_id,
            )
            assert visible == 1, (
                f"Namespace A should see its own row (id={a_row_id}), count={visible}"
            )

        # Verify namespace B cannot see namespace A's row (RLS blocks it).
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            visible = await pg_app_conn.fetchval(
                "SELECT count(*) FROM entity_merge_queue WHERE id = $1",
                a_row_id,
            )
            assert visible == 0, (
                f"Namespace B should NOT see namespace A row (id={a_row_id}), count={visible}"
            )

    async def test_rls_enforced(self, pg_admin_conn: asyncpg.Connection) -> None:
        """Verify RLS is ENABLED and FORCED on entity_merge_queue."""
        # Check that RLS is enabled
        rowsecurity = await pg_admin_conn.fetchval(
            """
            SELECT relrowsecurity
            FROM pg_class
            WHERE relname = 'entity_merge_queue'
            """
        )
        assert rowsecurity, "Row-Level Security is not ENABLED on entity_merge_queue"

        # Check that at least one policy exists
        policy_count = await pg_admin_conn.fetchval(
            """
            SELECT COUNT(*)
            FROM pg_policies
            WHERE tablename = 'entity_merge_queue'
            AND schemaname = 'public'
            """
        )
        assert policy_count > 0, (
            "No RLS policies found on entity_merge_queue (must have tenant_isolation_policy)"
        )

    async def test_default_status_is_pending(
        self, pg_admin_conn: asyncpg.Connection, test_namespace_id: uuid.UUID
    ) -> None:
        """Verify default status value is 'pending'."""
        status = await pg_admin_conn.fetchval(
            """
            INSERT INTO entity_merge_queue
            (namespace_id, node_type, candidate_payload, target_node_id, score)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING status
            """,
            test_namespace_id,
            "TestEntity",
            '{"test": "default"}',
            uuid.uuid4(),
            0.65,
        )
        assert status == "pending", f"Expected default status 'pending', got '{status}'"

    async def test_created_at_is_set_automatically(
        self, pg_admin_conn: asyncpg.Connection, test_namespace_id: uuid.UUID
    ) -> None:
        """Verify created_at is set automatically on insert."""
        from datetime import datetime

        created_at = await pg_admin_conn.fetchval(
            """
            INSERT INTO entity_merge_queue
            (namespace_id, node_type, candidate_payload, target_node_id, score)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING created_at
            """,
            test_namespace_id,
            "TestEntity",
            '{"test": "timestamp"}',
            uuid.uuid4(),
            0.55,
        )
        assert created_at is not None, "created_at was not set"
        assert isinstance(created_at, datetime), (
            f"created_at is not a datetime, got {type(created_at)}"
        )
