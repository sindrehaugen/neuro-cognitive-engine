"""
tests/unit/test_support_schema.py
=================================
Unit & catalog tests for M10 Support Engine schema (Wave 1 -- support-schema).

Verifies:
  1. `service_tickets`, `sla_clocks`, and `customer_health` are registered in
     `EXPECTED_TENANT_RLS_TABLES` mapping to `namespace_id`.
  2. Migration 065 carries required structural definitions, named constraints,
     and RLS policies.
  3. `nce/schema.sql` contains the matching table DDL.
  4. Live/scratch catalog check (when PG_DSN is reachable) confirms
     `relrowsecurity` and `relforcerowsecurity` on all three tables.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = REPO_ROOT / "nce" / "migrations" / "065_support_service_tickets.sql"
SCHEMA_FILE = REPO_ROOT / "nce" / "schema.sql"

SUPPORT_TABLES = ("service_tickets", "sla_clocks", "customer_health")


def test_support_tables_in_expected_tenant_rls_tables() -> None:
    """All three Support tables must be registered in EXPECTED_TENANT_RLS_TABLES."""
    for table in SUPPORT_TABLES:
        assert table in EXPECTED_TENANT_RLS_TABLES, (
            f"{table} missing from EXPECTED_TENANT_RLS_TABLES"
        )
        assert EXPECTED_TENANT_RLS_TABLES[table] == "namespace_id", (
            f"{table} ownership column is not namespace_id"
        )


def test_migration_065_exists_and_declares_tables() -> None:
    """Migration 065 must exist and contain DDL and RLS policies for all 3 tables."""
    assert MIGRATION_FILE.is_file(), f"Migration file missing: {MIGRATION_FILE}"
    content = MIGRATION_FILE.read_text(encoding="utf-8")

    for table in SUPPORT_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in content, (
            f"DDL for {table} missing in migration"
        )
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in content, (
            f"ENABLE RLS for {table} missing"
        )
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in content, (
            f"FORCE RLS for {table} missing"
        )
        assert f"CREATE POLICY tenant_isolation_policy ON {table}" in content, (
            f"Policy for {table} missing"
        )


def test_schema_sql_declares_support_tables() -> None:
    """nce/schema.sql must declare all three Support tables with RLS."""
    assert SCHEMA_FILE.is_file(), f"schema.sql missing: {SCHEMA_FILE}"
    content = SCHEMA_FILE.read_text(encoding="utf-8")

    for table in SUPPORT_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in content, (
            f"DDL for {table} missing in schema.sql"
        )
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in content, (
            f"ENABLE RLS for {table} missing in schema.sql"
        )
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in content, (
            f"FORCE RLS for {table} missing in schema.sql"
        )


def test_named_constraints_present_in_migration() -> None:
    """Every CHECK and constraint must be explicitly named to prevent catalog divergence."""
    content = MIGRATION_FILE.read_text(encoding="utf-8")

    expected_constraints = [
        "service_tickets_summary_not_blank",
        "service_tickets_source_check",
        "service_tickets_status_check",
        "service_tickets_priority_check",
        "service_tickets_change_origin_check",
        "sla_clocks_breach_type_check",
        "customer_health_customer_id_not_blank",
        "customer_health_score_range",
        "customer_health_churn_risk_check",
    ]
    for constraint_name in expected_constraints:
        assert f"CONSTRAINT {constraint_name}" in content, (
            f"Constraint {constraint_name} missing from migration 065"
        )


@pytest.mark.asyncio
async def test_database_catalog_has_active_rls_and_policies() -> None:
    """If PG_DSN is reachable, verify relrowsecurity and relforcerowsecurity in catalog."""
    import asyncpg

    dsn = os.getenv("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN not set; skipping live catalog check")

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        pytest.skip(f"Database not reachable at {dsn}: {exc}")

    try:
        for table in SUPPORT_TABLES:
            row = await conn.fetchrow(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = $1;
                """,
                table,
            )
            assert row is not None, f"Table {table} not found in database catalog"
            assert row["relrowsecurity"] is True, f"relrowsecurity is False on {table}"
            assert row["relforcerowsecurity"] is True, f"relforcerowsecurity is False on {table}"

            policies = await conn.fetch(
                """
                SELECT polname, polcmd
                FROM pg_policy pol
                JOIN pg_class c ON c.oid = pol.polrelid
                WHERE c.relname = $1;
                """,
                table,
            )
            policy_names = [p["polname"] for p in policies]
            assert "tenant_isolation_policy" in policy_names, (
                f"tenant_isolation_policy missing on {table}, found: {policy_names}"
            )
    finally:
        await conn.close()
