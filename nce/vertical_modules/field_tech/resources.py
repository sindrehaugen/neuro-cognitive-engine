r"""nce.vertical_modules.field_tech.resources — Resource definitions for Field Tech.

Lane E Wave E-10:
Registers C12 ResourceSpecs for Field Tech's owned node types in
node-ownership.json that have a real, tenant-scoped, single-table DDL:
  - WORK_ORDER (work_orders table -- NOT "field_tech_work_orders"; the prior
    exemption text named the wrong table, corrected in Lane E's E-9 sweep)
  - FIELD_TECH_TIME_ENTRY (time_entries table -- NOT "field_tech_time_entries",
    also corrected in E-9)
  - FIELD_TECH_CHECKLIST (checklists table)

FIELD_TECH_SCAN and FIELD_TECH_PHOTO stay exempted: no dedicated table exists
for either (grep -c "CREATE TABLE IF NOT EXISTS.*scan\|.*photo" nce/schema.sql
-> 0); both are kg_nodes-only event records. BOM_LINE stays exempted as the
cross-engine transition split (system_design/sales/procurement/inventory/
field_tech all share bom_line_content by lifecycle stage).
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

# 1. WORK_ORDER
WORK_ORDER_SPEC = ResourceSpec(
    engine="field_tech",
    entity="work-orders",
    node_type="WORK_ORDER",
    table_name="work_orders",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=(
        "status",
        "kind",
        "assignee_id",
        "location_id",
        "partner_scope_id",
        "source_kind",
    ),
    searchable_fields=("summary", "source_ref"),
    writable_fields=(
        "kind",
        "source_kind",
        "source_ref",
        "location_id",
        "assignee_id",
        "assignee_kind",
        "status",
        "priority",
        "summary",
        "due_at",
        "partner_scope_id",
    ),
    description="Field service work orders: install/service kind, dispatch status, and assignment.",
)
register_resource(WORK_ORDER_SPEC)


# 2. FIELD_TECH_TIME_ENTRY
FIELD_TECH_TIME_ENTRY_SPEC = ResourceSpec(
    engine="field_tech",
    entity="time-entries",
    node_type="FIELD_TECH_TIME_ENTRY",
    table_name="time_entries",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("work_order_id", "approved", "source", "partner_scope_id"),
    searchable_fields=(),
    writable_fields=(
        "work_order_id",
        "started_at",
        "ended_at",
        "source",
        "approved",
        "partner_scope_id",
    ),
    description="Technician time entries against a work order, GPS- or manually-sourced.",
)
register_resource(FIELD_TECH_TIME_ENTRY_SPEC)


# 3. FIELD_TECH_CHECKLIST
FIELD_TECH_CHECKLIST_SPEC = ResourceSpec(
    engine="field_tech",
    entity="checklists",
    node_type="FIELD_TECH_CHECKLIST",
    table_name="checklists",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("work_order_id", "partner_scope_id"),
    searchable_fields=("template_id",),
    writable_fields=("work_order_id", "template_id", "items", "completed_at", "partner_scope_id"),
    description="ISO9001 compliance checklists against a work order, with templated items.",
)
register_resource(FIELD_TECH_CHECKLIST_SPEC)
