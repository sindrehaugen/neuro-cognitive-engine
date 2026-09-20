"""
tests/unit/test_b7_quote_templates_and_packages.py
=====================================================
Coverage for Wave B-7: QUOTE_TEMPLATE (nce/vertical_modules/sales/resources.py)
and PACKAGE (nce/vertical_modules/product/resources.py). Both are genuinely
new node types (grepped for existing quote-template/package infrastructure
before building -- zero hits), so this is the first test either spec has ever
had -- exactly the gap test_system_design_resources.py's own docstring
identifies as how #311 shipped a real bug undetected.
"""

from __future__ import annotations

from nce.resource_surface import build_all_resource_tool_specs
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.product.resources import PACKAGE_SPEC
from nce.vertical_modules.sales.resources import QUOTE_TEMPLATE_SPEC


def test_quote_template_is_tenant_scoped_postgres() -> None:
    """A quote template is a tenant's own draft content, not a graph
    identity or a global shared fact."""
    assert QUOTE_TEMPLATE_SPEC.tenant_scope == "tenant"
    assert QUOTE_TEMPLATE_SPEC.table_name == "sales_quote_templates"
    assert QUOTE_TEMPLATE_SPEC.storage_kind == "postgres"


def test_package_is_tenant_scoped_not_global_like_product_sku() -> None:
    """Unlike PRODUCT_SKU's global product_catalog, a package is a tenant's
    own commercial bundling -- must NOT resolve to the "global" scope."""
    assert PACKAGE_SPEC.tenant_scope == "tenant"
    assert PACKAGE_SPEC.table_name == "product_packages"


def test_package_has_product_engine_guard() -> None:
    """product HAS a _guard.py -- a missing enabled_guard on an engine that
    has one is a hard CI failure (#296). PRODUCT_SKU already carries it;
    this pins that PACKAGE does too, so a future edit can't silently drop it."""
    assert PACKAGE_SPEC.enabled_guard is not None
    assert PACKAGE_SPEC.enabled_guard.__name__ == "require_product_enabled"


def test_quote_template_has_no_engine_guard() -> None:
    """sales has no _guard.py at all -- confirmed, not assumed."""
    assert QUOTE_TEMPLATE_SPEC.enabled_guard is None


def test_both_specs_generate_exactly_the_expected_c12_tools() -> None:
    all_specs = build_all_resource_tool_specs()
    quote_template_tools = {n for n in all_specs if "quote_templates" in n}
    package_tools = {n for n in all_specs if n.startswith("product_") and "packages" in n}

    assert quote_template_tools == {
        "sales_list_quote_templates",
        "sales_get_quote_templates",
        "sales_upsert_quote_templates",
        "sales_archive_quote_templates",
    }
    assert package_tools == {
        "product_list_packages",
        "product_get_packages",
        "product_upsert_packages",
        "product_archive_packages",
    }


def test_both_specs_tools_registered_with_correct_flags() -> None:
    expected = {
        "sales_list_quote_templates": {"cacheable": True, "admin_only": False, "mutation": False},
        "sales_get_quote_templates": {"cacheable": True, "admin_only": False, "mutation": False},
        "sales_upsert_quote_templates": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "sales_archive_quote_templates": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "product_list_packages": {"cacheable": True, "admin_only": False, "mutation": False},
        "product_get_packages": {"cacheable": True, "admin_only": False, "mutation": False},
        "product_upsert_packages": {"cacheable": False, "admin_only": False, "mutation": True},
        "product_archive_packages": {"cacheable": False, "admin_only": False, "mutation": True},
    }
    for tool_name, flags in expected.items():
        assert tool_name in TOOL_REGISTRY, f"{tool_name!r} not found in TOOL_REGISTRY"
        spec = TOOL_REGISTRY[tool_name]
        for flag, value in flags.items():
            actual = getattr(spec, flag)
            assert actual == value, f"{tool_name}.{flag}: expected {value!r}, got {actual!r}"


def test_package_writable_fields_do_not_leak_the_reserve_kit_expansion() -> None:
    """This wave builds the catalog record only -- writable_fields must not
    include anything that would imply a live reservation/expansion state
    (e.g. a status or reserved_at field), which this migration deliberately
    does not add."""
    assert set(PACKAGE_SPEC.writable_fields) == {"name", "description", "components"}


def test_quote_template_writable_fields_are_draft_only() -> None:
    assert set(QUOTE_TEMPLATE_SPEC.writable_fields) == {"name", "description", "template_lines"}
