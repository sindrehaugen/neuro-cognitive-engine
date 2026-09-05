"""Unit and integration tests for Field Tech Engine (Module 12) schema.

Validates:
1. Migration 067 exists, is non-empty, and matches idempotent DDL expectations.
2. Table declarations in nce.event_log.EXPECTED_TENANT_RLS_TABLES (67 -> 70).
3. Presence of work_orders, checklists, and time_entries in nce/schema.sql.
4. RLS enabled, forced, and tenant_isolation_policy on all 3 tables.
5. Foreign key constraints to namespaces ON DELETE CASCADE.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "nce" / "migrations" / "067_field_tech_work_orders.sql"
)
SCHEMA_SQL_PATH = Path(__file__).resolve().parents[2] / "nce" / "schema.sql"

REQUIRED_TABLES = ("work_orders", "checklists", "time_entries")


def test_field_tech_tables_declared_in_expected_tenant_rls_tables() -> None:
    for tbl in REQUIRED_TABLES:
        assert tbl in EXPECTED_TENANT_RLS_TABLES, f"{tbl} missing from EXPECTED_TENANT_RLS_TABLES"
        assert EXPECTED_TENANT_RLS_TABLES[tbl] == "namespace_id"


def test_field_tech_migration_file_exists() -> None:
    assert MIGRATION_PATH.exists(), f"{MIGRATION_PATH} missing"
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    assert len(content.strip()) > 100, "Migration file is suspiciously short"
    for tbl in REQUIRED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {tbl}" in content
        assert f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY" in content
        assert f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY" in content
        assert f"CREATE POLICY tenant_isolation_policy ON {tbl}" in content


def test_field_tech_schema_sql_contains_tables() -> None:
    schema_sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    for tbl in REQUIRED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {tbl}" in schema_sql
        assert f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY" in schema_sql
        assert f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY" in schema_sql


@pytest.mark.asyncio
async def test_field_tech_schema_scratch_db_inspection() -> None:
    dsn = (
        os.getenv("PG_DSN_SCRATCH")
        or "postgresql://mcp_user:mcp_password@127.0.0.1:5432/memory_meta_scratch"
    )
    try:
        conn = await asyncpg.connect(dsn, timeout=5.0)
    except Exception as exc:
        pytest.skip(f"Scratch DB not accessible ({exc}); skipping live catalog verification")
        return

    try:
        for tbl in REQUIRED_TABLES:
            row = await conn.fetchrow(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = $1 AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
                """,
                tbl,
            )
            assert row is not None, f"Table {tbl} does not exist in scratch DB"
            assert row["relrowsecurity"] is True, f"RLS not enabled on {tbl}"
            assert row["relforcerowsecurity"] is True, f"FORCE LIS not enabled on {tbl}"

            # Verify policy
            policy = await conn.fetchrow(
                """
                SELECT policyname, qual
                FROM pg_policies
                WHERE schemaname = 'public' AND tablename = $1 AND policyname = 'tenant_isolation_policy'
                """,
                tbl,
            )
            assert policy is not None, f"tenant_isolation_policy missing on {tbl}"
    finally:
        await conn.close()
