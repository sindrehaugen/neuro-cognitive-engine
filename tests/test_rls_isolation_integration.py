"""Postgres RLS integration: fail-fast namespace + cross-tenant isolation."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from nce.auth import _reset_rls_context, set_namespace_context


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_nce_namespace_fails_without_context(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await _reset_rls_context(conn)
            with pytest.raises(asyncpg.PostgresError, match="nce.namespace_id"):
                await conn.fetchval("SELECT get_nce_namespace()")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resource_quotas_cross_namespace_isolation(
    pg_app_conn,
    make_namespace,
) -> None:
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    resource_type = f"pytest-rls-{uuid4().hex}"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        row_id = await pg_app_conn.fetchval(
            """
            INSERT INTO resource_quotas (
                namespace_id, resource_type, limit_amount
            )
            VALUES ($1, $2, 10)
            RETURNING id
            """,
            ns_a,
            resource_type,
        )

    assert row_id is not None

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible = await pg_app_conn.fetchval(
            "SELECT count(*) FROM resource_quotas WHERE id = $1",
            row_id,
        )
        assert visible == 0

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible = await pg_app_conn.fetchval(
            "SELECT count(*) FROM resource_quotas WHERE id = $1",
            row_id,
        )
        assert visible == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_catalog_force_enabled(pg_app_conn) -> None:
    """FORCE ROW LEVEL SECURITY must be on for tenant tables (catalog probe)."""
    from nce.event_log import EXPECTED_TENANT_RLS_TABLES

    for table in EXPECTED_TENANT_RLS_TABLES:
        force_on = await pg_app_conn.fetchval(
            """
            SELECT c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = $1
            """,
            table,
        )
        assert force_on is True, f"{table}: FORCE RLS expected"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d365_integrations_cross_namespace_isolation(
    pg_app_conn,
    make_namespace,
) -> None:
    """Verify that d365_integrations table is properly isolated between namespaces by RLS."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    org_url = f"https://pytest-org-{uuid4().hex}.crm.dynamics.com"

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        row_id = await pg_app_conn.fetchval(
            """
            INSERT INTO d365_integrations (
                namespace_id, org_url, status
            )
            VALUES ($1, $2, 'ACTIVE')
            RETURNING id
            """,
            ns_a,
            org_url,
        )

    assert row_id is not None

    # Verify namespace B cannot see namespace A's integration
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible = await pg_app_conn.fetchval(
            "SELECT count(*) FROM d365_integrations WHERE id = $1",
            row_id,
        )
        assert visible == 0

    # Verify namespace A can see its own integration
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible = await pg_app_conn.fetchval(
            "SELECT count(*) FROM d365_integrations WHERE id = $1",
            row_id,
        )
        assert visible == 1
