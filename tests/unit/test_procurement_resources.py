"""
tests/unit/test_procurement_resources.py
==========================================
Coverage for nce/vertical_modules/procurement/resources.py's DEAL_REGISTRATION
spec (Wave B-15). PO_LINE, the module's other spec, has no dedicated test of
its own either -- not fixed here (out of scope for this wave), but noted so
the gap isn't silently widened to look intentional.
"""

from __future__ import annotations

from nce.resource_surface import build_all_resource_tool_specs
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.procurement.resources import DEAL_REGISTRATION_SPEC


def test_deal_registration_spec_is_tenant_scoped_postgres() -> None:
    """A registered deal is a discrete business record, not a graph identity --
    storage_kind defaults to postgres (not kg_nodes), and the table is real."""
    assert DEAL_REGISTRATION_SPEC.tenant_scope == "tenant"
    assert DEAL_REGISTRATION_SPEC.table_name == "procurement_deal_registrations"
    assert DEAL_REGISTRATION_SPEC.storage_kind == "postgres"


def test_deal_registration_supplier_is_plain_text_not_an_invented_fk() -> None:
    """Precedent (procurement_bid_prices.leverandor) beats invention: supplier
    is writable free text, not a foreign key to a suppliers table that does
    not exist anywhere in this schema."""
    assert "supplier" in DEAL_REGISTRATION_SPEC.writable_fields
    assert "supplier" in DEAL_REGISTRATION_SPEC.filterable_fields


def test_deal_registration_generates_exactly_four_c12_tools() -> None:
    """list/get/upsert/archive, and no more -- a fifth tool here would mean
    the spec grew a capability nobody asked for."""
    all_specs = build_all_resource_tool_specs()
    names = {n for n in all_specs if n.startswith("procurement_") and "deal_registration" in n}
    assert names == {
        "procurement_list_deal_registrations",
        "procurement_get_deal_registrations",
        "procurement_upsert_deal_registrations",
        "procurement_archive_deal_registrations",
    }


def test_deal_registration_tools_registered_with_correct_flags() -> None:
    """list/get are cacheable reads; upsert/archive are mutations. None are
    admin_only -- this resource has no enabled_guard and no tier restriction,
    matching PO_LINE's precedent (procurement has no _guard.py)."""
    expected = {
        "procurement_list_deal_registrations": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "procurement_get_deal_registrations": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "procurement_upsert_deal_registrations": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "procurement_archive_deal_registrations": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
    }
    for tool_name, flags in expected.items():
        assert tool_name in TOOL_REGISTRY, f"{tool_name!r} not found in TOOL_REGISTRY"
        spec = TOOL_REGISTRY[tool_name]
        for flag, value in flags.items():
            actual = getattr(spec, flag)
            assert actual == value, f"{tool_name}.{flag}: expected {value!r}, got {actual!r}"


def test_deal_registration_has_no_enabled_guard() -> None:
    """procurement has no _guard.py -- a missing enabled_guard on an engine
    that DOES have one is a hard CI failure (#296); confirming the inverse
    is deliberate, not an oversight, for this specific spec."""
    assert DEAL_REGISTRATION_SPEC.enabled_guard is None
