"""
tests/unit/test_b3_deal_participant.py
=========================================
Coverage for Wave B-3's sub-resource half: DEAL_PARTICIPANT
(nce/vertical_modules/sales/resources.py). A genuinely new node type --
grepped node-ownership.json/sales/resources.py/schema.sql before building --
zero hits anywhere. First test this spec has ever had -- exactly the gap
test_system_design_resources.py's own docstring identifies as how #311
shipped a real bug undetected.

DEAL_TAG, B-3's sibling ask, is declined as already satisfied by the generic
`/api/{entity}/{id}/tags` sub-resource route every registered spec gets for
free -- not built, so not tested here. See CHARTER_WAVE_AUDIT.md's B-3 row.
"""

from __future__ import annotations

from nce.resource_surface import build_all_resource_tool_specs
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.sales.resources import DEAL_PARTICIPANT_SPEC


def test_deal_participant_is_tenant_scoped_postgres() -> None:
    """A deal participant is a tenant's own pipeline data, not a graph
    identity or a global shared fact."""
    assert DEAL_PARTICIPANT_SPEC.tenant_scope == "tenant"
    assert DEAL_PARTICIPANT_SPEC.table_name == "sales_deal_participants"
    assert DEAL_PARTICIPANT_SPEC.storage_kind == "postgres"
    assert DEAL_PARTICIPANT_SPEC.id_field == "id"


def test_deal_participant_has_no_engine_guard() -> None:
    """sales has no _guard.py at all -- confirmed, not assumed."""
    assert DEAL_PARTICIPANT_SPEC.enabled_guard is None


def test_deal_participant_generates_exactly_the_expected_c12_tools() -> None:
    all_specs = build_all_resource_tool_specs()
    participant_tools = {n for n in all_specs if "deal_participants" in n}

    assert participant_tools == {
        "sales_list_deal_participants",
        "sales_get_deal_participants",
        "sales_upsert_deal_participants",
        "sales_archive_deal_participants",
    }


def test_deal_participant_tools_registered_with_correct_flags() -> None:
    expected = {
        "sales_list_deal_participants": {"cacheable": True, "admin_only": False, "mutation": False},
        "sales_get_deal_participants": {"cacheable": True, "admin_only": False, "mutation": False},
        "sales_upsert_deal_participants": {
            "cacheable": False,
            "admin_only": False,
            "mutation": True,
        },
        "sales_archive_deal_participants": {
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


def test_deal_participant_writable_fields_include_the_parent_fk() -> None:
    """deal_id must be writable -- it is how a participant is attached to a
    deal at create time, unlike a satellite table where the join is implicit."""
    assert set(DEAL_PARTICIPANT_SPEC.writable_fields) == {
        "deal_id",
        "participant_name",
        "participant_email",
        "role",
        "metadata",
    }


def test_deal_participant_filterable_fields_include_deal_id() -> None:
    """Listing participants scoped to one deal is the primary access pattern."""
    assert "deal_id" in DEAL_PARTICIPANT_SPEC.filterable_fields


def test_deal_participant_external_tiers_see_nothing() -> None:
    """Deal stakeholder contact info is internal pipeline data -- external-
    customer and contractor callers get zero fields, matching RESOURCE_SPEC/
    FUNCTIONAL_LOCATION_SPEC's explicit-empty-not-omitted precedent."""
    assert DEAL_PARTICIPANT_SPEC.tier_allowlists["contractor"] == ()
    assert DEAL_PARTICIPANT_SPEC.tier_allowlists["external-customer"] == ()
