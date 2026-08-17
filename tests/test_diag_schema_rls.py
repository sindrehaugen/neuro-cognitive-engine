"""Integration tests for migration 025 — diag-schema (Batch 67).

Acceptance gate:
  (a) RLS denies a cross-namespace read of diag_ingestions:
      insert a row as namespace A, confirm namespace B cannot see it.
  (b) topology_graph.relforcerowsecurity = true in pg_class
      (proves the Domain-5/D1 FORCE fix is applied).

Run with:
  NCE_INTEGRATION_PG_DSN=postgresql://mcp_user:mcp_password@127.0.0.1:5433/memory_meta \\
  python -m pytest -m integration tests/test_diag_schema_rls.py -q
"""

from __future__ import annotations

import os
import pathlib
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Fixture: isolated connection that applies 025_diagnostics.sql once per
# session.  Uses the same pg_pool fixture from conftest.py as base.
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent / "nce" / "migrations" / "025_diagnostics.sql"
)


@pytest_asyncio.fixture
async def applied_pool():
    """Pool on the integration DB with migration 025 applied (idempotent).

    Scope is function (not module) to match asyncio_default_fixture_loop_scope
    in pytest.ini (strict asyncio mode).  The migration SQL uses IF NOT EXISTS
    throughout so repeated application per-test is safe.
    """
    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        pytest.skip("Integration tests need NCE_INTEGRATION_PG_DSN, PG_DSN, or DATABASE_URL")

    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=60)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable: {exc}")

    # Apply the migration (all DDL is idempotent via IF NOT EXISTS / DO $$).
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)

    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def diag_owner_conn(applied_pool: asyncpg.Pool):
    """Owner-role connection (mcp_user) that can INSERT without RLS restriction."""
    async with applied_pool.acquire() as conn:
        yield conn


@pytest_asyncio.fixture
async def diag_app_conn(applied_pool: asyncpg.Pool):
    """Application-role connection (nce_app) that is subject to RLS.

    Falls back to the pool connection if nce_app is not reachable,
    in which case RLS tests that depend on role separation are skipped.
    """
    from urllib.parse import urlparse, urlunparse

    primary = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()

    try:
        parsed = urlparse(primary)
        host = parsed.hostname or "127.0.0.1"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        app_dsn = urlunparse(parsed._replace(netloc=f"nce_app:nce_app_secret@{host}"))
        app_pool = await asyncpg.create_pool(app_dsn, min_size=1, max_size=2, command_timeout=30)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"nce_app role not reachable (need separate role for RLS test): {exc}")

    try:
        async with app_pool.acquire() as conn:
            yield conn
    finally:
        await app_pool.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _make_ns(conn: asyncpg.Connection) -> object:
    slug = f"pytest-diag-{uuid4().hex}"
    return await conn.fetchval("INSERT INTO namespaces (slug) VALUES ($1) RETURNING id", slug)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_025_tables_exist(applied_pool: asyncpg.Pool) -> None:
    """All three diag tables must exist after migration 025 is applied."""
    async with applied_pool.acquire() as conn:
        for table in ("diag_ingestions", "diag_anomalies", "device_health_rollup"):
            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM   information_schema.tables
                    WHERE  table_schema = 'public'
                      AND  table_name   = $1
                )
                """,
                table,
            )
            assert exists, f"Table {table!r} missing after migration 025"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topology_graph_force_rls(applied_pool: asyncpg.Pool) -> None:
    """(b) topology_graph must have relforcerowsecurity = true after migration 025.

    This proves the Domain-5/D1 HIGH security fix: previously the table owner /
    nce_gc role could bypass the topology_graph_tenant_isolation policy.
    """
    async with applied_pool.acquire() as conn:
        force_on = await conn.fetchval(
            """
            SELECT c.relforcerowsecurity
            FROM   pg_class c
            JOIN   pg_namespace n ON n.oid = c.relnamespace
            WHERE  n.nspname = 'public'
              AND  c.relname  = 'topology_graph'
            """
        )
    assert force_on is True, (
        "topology_graph: FORCE ROW LEVEL SECURITY expected (Domain-5/D1 security fix) "
        "but relforcerowsecurity = False"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diag_tables_rls_enabled_and_forced(applied_pool: asyncpg.Pool) -> None:
    """RLS must be both ENABLED and FORCEd on all three diag tables."""
    async with applied_pool.acquire() as conn:
        for table in ("diag_ingestions", "diag_anomalies", "device_health_rollup"):
            row = await conn.fetchrow(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM   pg_class c
                JOIN   pg_namespace n ON n.oid = c.relnamespace
                WHERE  n.nspname = 'public'
                  AND  c.relname  = $1
                """,
                table,
            )
            assert row is not None, f"Table {table!r} not found in pg_class"
            assert row["relrowsecurity"] is True, f"{table}: ENABLE RLS expected"
            assert row["relforcerowsecurity"] is True, f"{table}: FORCE RLS expected"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diag_ingestions_cross_namespace_deny(
    diag_app_conn: asyncpg.Connection,
    applied_pool: asyncpg.Pool,
) -> None:
    """(a) RLS must deny a cross-namespace read of diag_ingestions.

    1. Insert a diag_ingestions row as namespace A (owner conn bypasses RLS).
    2. Via nce_app role with namespace B set, assert the row is invisible.
    3. Via nce_app role with namespace A set, assert the row IS visible (sanity).
    """
    # Create two namespaces using the owner pool (no RLS restriction)
    async with applied_pool.acquire() as owner:
        ns_a = await _make_ns(owner)
        ns_b = await _make_ns(owner)

        # Insert a diag_ingestions row directly (owner bypasses RLS)
        row_id = await owner.fetchval(
            """
            INSERT INTO diag_ingestions (namespace_id, ingest_id, source, status)
            VALUES ($1, $2, 'upload', 'PENDING')
            RETURNING id
            """,
            ns_a,
            f"test-ingest-{uuid4().hex}",
        )
    assert row_id is not None

    # --- Cross-namespace deny (namespace B must NOT see namespace A's row) ---
    # SET does not accept $1 parameters in Postgres; use set_config() instead
    # (matches the pattern in nce.auth.set_namespace_context).
    app = diag_app_conn
    async with app.transaction():
        await app.execute(
            "SELECT set_config('nce.namespace_id', $1, true)",
            str(ns_b),
        )
        visible_b = await app.fetchval("SELECT count(*) FROM diag_ingestions WHERE id = $1", row_id)
    assert visible_b == 0, (
        f"RLS BREACH: namespace B can read diag_ingestions row belonging to namespace A "
        f"(row_id={row_id}, ns_a={ns_a}, ns_b={ns_b})"
    )

    # --- Same-namespace visibility (namespace A MUST see its own row) ---
    async with app.transaction():
        await app.execute(
            "SELECT set_config('nce.namespace_id', $1, true)",
            str(ns_a),
        )
        visible_a = await app.fetchval("SELECT count(*) FROM diag_ingestions WHERE id = $1", row_id)
    assert visible_a == 1, (
        f"RLS ERROR: namespace A cannot read its own diag_ingestions row "
        f"(row_id={row_id}, ns_a={ns_a})"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_uq_topology_edge_index_exists(applied_pool: asyncpg.Pool) -> None:
    """uq_topology_edge unique index must exist on topology_graph after migration 025."""
    async with applied_pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM   pg_indexes
                WHERE  schemaname = 'public'
                  AND  tablename  = 'topology_graph'
                  AND  indexname  = 'uq_topology_edge'
            )
            """
        )
    assert exists, "uq_topology_edge unique index missing on topology_graph"
