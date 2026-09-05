"""Unit tests for Module 14 Marketing Engine schema and RLS configuration."""

from __future__ import annotations

from pathlib import Path

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_marketing_tables_registered_in_expected_tenant_rls_tables():
    """All 3 Marketing Engine tables must be registered in EXPECTED_TENANT_RLS_TABLES."""
    expected_marketing_tables = {
        "case_studies": "namespace_id",
        "testimonials": "namespace_id",
        "content_assets": "namespace_id",
    }
    for table, col in expected_marketing_tables.items():
        assert table in EXPECTED_TENANT_RLS_TABLES, (
            f"Table {table!r} missing from EXPECTED_TENANT_RLS_TABLES"
        )
        assert EXPECTED_TENANT_RLS_TABLES[table] == col, (
            f"Table {table!r} has namespace column {EXPECTED_TENANT_RLS_TABLES[table]!r}, expected {col!r}"
        )


def test_expected_tenant_rls_tables_total_count():
    """Verify total count of tenant RLS tables after Marketing Engine addition is 73."""
    assert len(EXPECTED_TENANT_RLS_TABLES) == 73, (
        f"Expected 73 tenant RLS tables, got {len(EXPECTED_TENANT_RLS_TABLES)}"
    )


def test_migration_and_schema_sql_contain_marketing_tables():
    """Both migration 069 and schema.sql must define the three Marketing tables with RLS and tenant policy."""
    migration_file = REPO_ROOT / "nce" / "migrations" / "069_marketing_engine.sql"
    schema_file = REPO_ROOT / "nce" / "schema.sql"

    assert migration_file.exists(), f"Migration file missing: {migration_file}"

    mig_text = migration_file.read_text(encoding="utf-8")
    schema_text = schema_file.read_text(encoding="utf-8")

    for table in ("case_studies", "testimonials", "content_assets"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in mig_text
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in mig_text
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in mig_text
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema_text
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in schema_text
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in schema_text
