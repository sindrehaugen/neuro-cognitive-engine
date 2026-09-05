"""Unit tests for Module 15 Staff & Resources Engine schema and RLS configuration."""

from __future__ import annotations

from pathlib import Path

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]

M15_TABLES = [
    "resources",
    "allocations",
    "travel_legs",
    "stays",
    "per_diems",
]


def test_resources_tables_registered_in_expected_tenant_rls_tables():
    """All M15 tables must be registered in EXPECTED_TENANT_RLS_TABLES."""
    for table in M15_TABLES:
        assert table in EXPECTED_TENANT_RLS_TABLES, (
            f"Table {table!r} missing from EXPECTED_TENANT_RLS_TABLES"
        )
        assert EXPECTED_TENANT_RLS_TABLES[table] == "namespace_id", (
            f"Table {table!r} has namespace column {EXPECTED_TENANT_RLS_TABLES[table]!r}, expected 'namespace_id'"
        )


def test_expected_tenant_rls_tables_total_count():
    """Verify total count of tenant RLS tables after Customer Portal Engine Phase 1 is 85."""
    assert len(EXPECTED_TENANT_RLS_TABLES) == 85, (
        f"Expected 85 tenant RLS tables, got {len(EXPECTED_TENANT_RLS_TABLES)}"
    )


def test_migrations_and_schema_sql_contain_all_resources_tables():
    """Both migrations (070, 071) and schema.sql must define all tables with RLS and tenant policy."""
    mig_070 = REPO_ROOT / "nce" / "migrations" / "070_resources_registry.sql"
    mig_071 = REPO_ROOT / "nce" / "migrations" / "071_allocations_btree_gist.sql"
    schema_file = REPO_ROOT / "nce" / "schema.sql"

    assert mig_070.exists(), f"Migration file missing: {mig_070}"
    assert mig_071.exists(), f"Migration file missing: {mig_071}"

    mig_070_text = mig_070.read_text(encoding="utf-8")
    mig_071_text = mig_071.read_text(encoding="utf-8")
    schema_text = schema_file.read_text(encoding="utf-8")

    # 070 checks
    assert "CREATE TABLE IF NOT EXISTS resources" in mig_070_text
    assert "ALTER TABLE resources ENABLE ROW LEVEL SECURITY" in mig_070_text
    assert "ALTER TABLE resources FORCE ROW LEVEL SECURITY" in mig_070_text
    assert "tenant_isolation_policy" in mig_070_text

    # 071 checks
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in mig_071_text
    for table in ["allocations", "travel_legs", "stays", "per_diems"]:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in mig_071_text
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in mig_071_text
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in mig_071_text
        assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nce_app" in mig_071_text

    # Exclusion constraint check in 071
    assert "EXCLUDE USING gist" in mig_071_text
    assert "exclude_resource_double_booking" in mig_071_text

    # schema.sql checks
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in schema_text
    for table in M15_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema_text
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in schema_text
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in schema_text
