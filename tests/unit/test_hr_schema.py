"""Unit tests for Module 13 HR Engine schema and RLS configuration."""

from __future__ import annotations

from nce.event_log import EXPECTED_TENANT_RLS_TABLES


def test_hr_tables_registered_in_expected_tenant_rls_tables():
    """All 4 HR Engine tables must be registered in EXPECTED_TENANT_RLS_TABLES."""
    expected_hr_tables = {
        "employees": "namespace_id",
        "skills": "namespace_id",
        "certifications": "namespace_id",
        "absences": "namespace_id",
    }
    for table, col in expected_hr_tables.items():
        assert table in EXPECTED_TENANT_RLS_TABLES, (
            f"Table {table!r} missing from EXPECTED_TENANT_RLS_TABLES"
        )
        assert EXPECTED_TENANT_RLS_TABLES[table] == col, (
            f"Table {table!r} has namespace column {EXPECTED_TENANT_RLS_TABLES[table]!r}, expected {col!r}"
        )


def test_expected_tenant_rls_tables_total_count():
    """Verify total count of tenant RLS tables after C17 sites, Wave D-5 support_ticket_actions, C12 sales, Wave B-9 agreements additions, Wave B-15 procurement_deal_registrations, and Wave B-7 (sales_quote_templates, product_packages), and Wave B-12 (economy_billing_runs/economy_billing_candidates/economy_billing_candidate_lines), and Wave C-4 (system_design_design_requests) is 117."""
    assert len(EXPECTED_TENANT_RLS_TABLES) == 117, (
        f"Expected 117 tenant RLS tables, got {len(EXPECTED_TENANT_RLS_TABLES)}"
    )
