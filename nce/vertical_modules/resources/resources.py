"""nce.vertical_modules.resources.resources — Resource definitions for Resources.

Lane E Wave E-6:
Registers C12 ResourceSpecs for Resources' owned node types in
node-ownership.json that have a real, tenant-scoped, single-table DDL and no
naming collision with an existing tool:
  - ALLOCATION (allocations table)
  - TRAVEL_LEG (travel_legs table)

RESOURCE stays exempted (see resource_surface/exemptions.py): its node type
name equals the engine name, so entity="resources" would generate an MCP
tool named resources_list_resources -- which collides with, and silently
overwrites via TOOL_REGISTRY.update(), the existing hand-written tool of
that exact name (nce/tool_registry.py, handle_resources_list_resources).
Found by diffing the exact TOOL_REGISTRY key set before/after registering
the spec, not by assuming the usual +4 delta. get/upsert/archive for this
node type would NOT collide (the existing hand-written tools use the
singular "resources_get_resource" etc.), only list does; reported rather
than silently renaming the entity to dodge it, since that also changes
REST paths and isn't this lane's call to make alone.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

# 1. ALLOCATION
ALLOCATION_SPEC = ResourceSpec(
    engine="resources",
    entity="allocations",
    node_type="ALLOCATION",
    table_name="allocations",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("resource_id", "demand_kind", "demand_id", "status"),
    searchable_fields=("demand_kind",),
    writable_fields=(
        "resource_id",
        "demand_kind",
        "demand_id",
        "functional_location_id",
        "starts_at",
        "ends_at",
        "status",
        "confidence",
        "attrs",
    ),
    description="Time-window resource booking against a demand source, with exclusion-guarded double-booking.",
)
register_resource(ALLOCATION_SPEC)


# 2. TRAVEL_LEG
TRAVEL_LEG_SPEC = ResourceSpec(
    engine="resources",
    entity="travel-legs",
    node_type="TRAVEL_LEG",
    table_name="travel_legs",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("allocation_id", "status", "mode"),
    searchable_fields=("origin", "destination", "booking_ref"),
    writable_fields=(
        "allocation_id",
        "origin",
        "destination",
        "departure_at",
        "arrival_at",
        "mode",
        "cost_nok",
        "booking_ref",
        "status",
        "attrs",
    ),
    description="Travel route legs (flight/train/car) associated with an allocation.",
)
register_resource(TRAVEL_LEG_SPEC)
