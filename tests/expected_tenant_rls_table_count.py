"""
tests/expected_tenant_rls_table_count.py
=========================================
K-consolidation: single source of truth for the
"how many entries are in nce.event_log.EXPECTED_TENANT_RLS_TABLES" pin
(2026-09-20).

Before this module, tests/unit/test_business_insights_schema.py,
test_customer_portal_spine.py, test_hr_schema.py, test_marketing_schema.py,
and test_resources_schema.py each independently hand-typed the same
literal in a test named test_expected_tenant_rls_tables_total_count,
along with a near-duplicate wave-history docstring. Any wave that changed
EXPECTED_TENANT_RLS_TABLES had to remember all five files, not just the
one it was actually working in -- this caused ~6 repeated fix cycles
across 3 lanes in a single day.

Masking-hypothesis check performed BEFORE this consolidation (per the
tests/tool_pins.py precedent, which found the pinned literals it replaced
had been silently absorbing a real detection gap): here they were not.
tests/test_docs_consistency.py::test_multi_tenancy_inventory_table_covers_every_tenant_table
already asserts FULL SET equality (both "missing" and "extra") between
EXPECTED_TENANT_RLS_TABLES and docs/multi_tenancy.md's per-domain
inventory -- independent of these five files and strictly stronger than
a bare count. A same-wave add-one/remove-one that left the count
unchanged would already be caught there. So this duplication was a pure
five-copies update tax, not a hidden gap: consolidating it removes the
tax without changing what is actually verified.

Every file below still imports this constant and still asserts it
against the live EXPECTED_TENANT_RLS_TABLES exactly as before -- only
the number of places a lane has to know about is (five -> one).
"""

from __future__ import annotations

EXPECTED_TENANT_RLS_TABLE_COUNT = 117
