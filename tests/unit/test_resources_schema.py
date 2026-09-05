"""Unit tests for Module 15 Staff & Resources Engine schema and RLS configuration."""

from __future__ import annotations

from pathlib import Path

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_resources_table_registered_in_expected_tenant_rls_tables():
    """The resources table must be registered in EXPECTED_TENANT_RLS_TABLES."""
    assert "resources" in EXPECTED_TENANT_RLS_TABLES, (
        "Table 'resources' missing from EXPECTED_TENANT_RLS_TABLES"
    )
    assert EXPECTED_TENANT_RLS_TABLES["resources"] == "namespace_id", (
        f"Table 'resources' has namespace column {EXPECTED_TENANT_RLS_TABLES['resources']!r}, expected 'namespace_id'"
    )


def test_expected_tenant_rls_tables_total_count():
    """Verify total count of tenant RLS tables after Resources Engine Phase 1 is 78."""
    assert len(EXPECTED_TENANT_RLS_TABLES) == 78, (
        f"Expected 78 tenant RLS tables, got {len(EXPECTED_TENANT_RLS_TABLES)}"
    )


def test_migration_and_schema_sql_contain_resources_table():
    """Both migration 070 and schema.sql must define the resources table with RLS and tenant policy."""
    migration_file = REPO_ROOT / "nce" / "migrations" / "070_resources_registry.sql"
    schema_file = REPO_ROOT / "nce" / "schema.sql"

    assert migration_file.exists(), f"Migration file missing: {migration_file}"

    mig_text = migration_file.read_text(encoding="utf-8")
    schema_text = schema_file.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS resources" in mig_text
    assert "ALTER TABLE resources ENABLE ROW LEVEL SECURITY" in mig_text
    assert "ALTER TABLE resources FORCE ROW LEVEL SECURITY" in mig_text
    assert "tenant_isolation_policy" in mig_text

    assert "CREATE TABLE IF NOT EXISTS resources" in schema_text
    assert "ALTER TABLE resources ENABLE ROW LEVEL SECURITY" in schema_text
    assert "ALTER TABLE resources FORCE ROW LEVEL SECURITY" in schema_text
