"""
tests/unit/test_system_design_resources.py
=============================================
Coverage for nce/vertical_modules/system_design/resources.py -- previously
zero (grep confirmed: no test anywhere imported this module or referenced
DEVICE_SPEC/PORT_SPEC/RACK_SPEC/CABLE_SPEC). That gap is exactly how #311
shipped: nothing ever constructed these four specs in a test, so nothing
ever exercised the filterable_fields/searchable_fields spec.py now
validates at declaration time.

Fix wave (2026-09-20, dispatched to lane G, unrelated to v1.6's struck
backfill-importer scope): DEVICE/PORT/RACK/CABLE originally declared
filterable/searchable fields naming secondary-table columns that do not
exist on kg_nodes -- an undefined-column SQL error against real Postgres,
invisible against the memory-store CI fallback. Trimmed to kg_nodes' own
real columns (change_origin), matching CONTACT's (#314) already-correct
pattern.
"""

from __future__ import annotations

from nce.vertical_modules.system_design.resources import (
    CABLE_SPEC,
    DEVICE_SPEC,
    PORT_SPEC,
    RACK_SPEC,
)

_ALL_FOUR = (DEVICE_SPEC, PORT_SPEC, RACK_SPEC, CABLE_SPEC)


def test_all_four_specs_construct_and_are_graph_scoped():
    for spec in _ALL_FOUR:
        assert spec.tenant_scope == "graph"
        assert spec.table_name is None


def test_filterable_and_searchable_fields_are_kg_nodes_columns_only():
    """#311 regression guard: every filterable/searchable field on these
    four specs must be a real kg_nodes column, not a satellite-table
    column. spec.py's __post_init__ already enforces this at import time
    (so a regression here would fail collection, not just this assertion)
    -- this test additionally pins the exact expected value, so a future
    edit that silently reintroduces a bad-but-still-kg_nodes-shaped field
    name is still caught.
    """
    for spec in _ALL_FOUR:
        assert spec.filterable_fields == ("change_origin",), spec.entity
        assert spec.searchable_fields == (), spec.entity


def test_satellite_fields_stay_writable_and_readable_via_secondary_tables():
    """Trimming filterable/searchable fields must not have also dropped the
    satellite columns from writable_fields/secondary_tables -- the get-by-id
    and create/patch paths still need them. Only filtering and searching on
    them is unsupported (see resources.py's module docstring)."""
    device_capability_fields = {f for st in DEVICE_SPEC.secondary_tables for f in st.fields}
    assert "device_category" in device_capability_fields
    assert "device_category" in DEVICE_SPEC.writable_fields

    port_capability_fields = {f for st in PORT_SPEC.secondary_tables for f in st.fields}
    assert "signal_format" in port_capability_fields
    assert "signal_format" in PORT_SPEC.writable_fields

    cable_state_fields = {f for st in CABLE_SPEC.secondary_tables for f in st.fields}
    assert "status" in cable_state_fields
    assert "status" in CABLE_SPEC.writable_fields
