"""Integration tests for node_ownership_registry table (Contract-A registry)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from nce.auth import set_namespace_context


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_ownership_registry_table_exists(pg_app_conn) -> None:
    """Verify the table exists."""
    exists = await pg_app_conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = 'node_ownership_registry'
        )
        """
    )
    assert exists is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_ownership_registry_force_rls_enabled(pg_app_conn) -> None:
    """Verify FORCE ROW LEVEL SECURITY is on."""
    force_on = await pg_app_conn.fetchval(
        """
        SELECT c.relforcerowsecurity
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'node_ownership_registry'
        """
    )
    assert force_on is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_ownership_registry_cross_namespace_isolation(
    pg_app_conn,
    make_namespace,
) -> None:
    """Verify rows written under namespace A are invisible from namespace B."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    node_type = f"test-node-type-{uuid4().hex[:8]}"

    # Write a row under namespace A.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        row_id = await pg_app_conn.fetchval(
            """
            INSERT INTO node_ownership_registry (
                namespace_id, node_type, owner_engine
            )
            VALUES ($1, $2, 'test-engine-a')
            RETURNING id
            """,
            ns_a,
            node_type,
        )

    assert row_id is not None

    # Verify the row is visible from namespace A.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible = await pg_app_conn.fetchval(
            "SELECT count(*) FROM node_ownership_registry WHERE id = $1",
            row_id,
        )
        assert visible == 1

    # Verify the row is invisible from namespace B (RLS blocks it).
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible = await pg_app_conn.fetchval(
            "SELECT count(*) FROM node_ownership_registry WHERE id = $1",
            row_id,
        )
        assert visible == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_ownership_registry_with_transition(
    pg_app_conn,
    make_namespace,
) -> None:
    """Verify transition column (optional per-transition writer-of-record)."""
    ns = await make_namespace()
    node_type = f"test-node-with-transition-{uuid4().hex[:8]}"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns)
        row_id = await pg_app_conn.fetchval(
            """
            INSERT INTO node_ownership_registry (
                namespace_id, node_type, transition, owner_engine
            )
            VALUES ($1, $2, 'update', 'test-engine-update')
            RETURNING id
            """,
            ns,
            node_type,
        )

    assert row_id is not None

    # Fetch back and verify all columns.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns)
        row = await pg_app_conn.fetchrow(
            "SELECT namespace_id, node_type, transition, owner_engine FROM node_ownership_registry WHERE id = $1",
            row_id,
        )

    assert row is not None
    assert row["node_type"] == node_type
    assert row["transition"] == "update"
    assert row["owner_engine"] == "test-engine-update"
