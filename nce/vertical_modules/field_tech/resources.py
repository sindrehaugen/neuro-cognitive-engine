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
from nce.vertical_modules.field_tech._guard import require_field_tech_enabled

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
        # work_order_id: found live 2026-09-20 (UNCREATABLE_SPECS.md) -- was
        # absent from writable_fields entirely, and the column is NOT NULL
        # with no default, so the generated create route always
        # NotNullViolationError'd, unconditionally. The real writer
        # (field_tech/work_orders.py::do_create_work_order) treats it as
        # caller-optional with a server-generated "WO-<hex>" fallback on
        # omission; the generated surface has no mechanism to replicate
        # that fallback (only id_field gets a uuid4() default), so this
        # field is effectively required through REST/MCP -- the same,
        # already-accepted asymmetry hr:absences.absence_id has.
        "work_order_id",
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
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column that work_orders does not have. status's
    # own CHECK enum (draft/scheduled/dispatched/in_progress/completed/
    # cancelled) already covers work-order lifecycle termination
    # ("cancelled") and is already writable via PATCH -- not a hidden
    # soft-delete gap, a workflow state this spec already exposes. No
    # hand-written archive path either. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
    description="Field service work orders: install/service kind, dispatch status, and assignment.",
    enabled_guard=require_field_tech_enabled,
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
    # "approved" is deliberately NOT writable here (C-7, 2026-09-20). It used
    # to be a plain writable_fields entry -- any caller with generic PATCH
    # access could flip it with no confirmation and no audit. Approval now
    # goes through POST /{id}/approve (field_tech/time_entry.py's
    # do_approve_time_entry, @governed, confirm-first, audited to
    # event_log). Re-adding "approved" here would reopen the exact gap the
    # governed route exists to close -- a front door beside an open window.
    writable_fields=(
        # time_entry_id: same shape and same fix as work_order_id above
        # (UNCREATABLE_SPECS.md) -- absent from writable_fields, NOT NULL
        # no default, real writer (field_tech/time_entry.py) falls back to
        # a server-generated "TE-<hex>" only outside the generated surface.
        "time_entry_id",
        "work_order_id",
        "started_at",
        "ended_at",
        "source",
        "partner_scope_id",
    ),
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column that time_entries does not have; no
    # alternate soft-delete column or hand-written archive path either. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
    description=(
        "Technician time entries against a work order, GPS- or manually-sourced. "
        "Approval is governed: POST /{id}/approve, not PATCH."
    ),
    enabled_guard=require_field_tech_enabled,
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
    writable_fields=(
        # checklist_id: same shape and same fix as work_order_id/
        # time_entry_id above (UNCREATABLE_SPECS.md) -- absent from
        # writable_fields, NOT NULL no default, real writer
        # (field_tech/checklist.py) falls back to a server-generated
        # "CL-<hex>" only outside the generated surface.
        "checklist_id",
        "work_order_id",
        "template_id",
        "items",
        "completed_at",
        "partner_scope_id",
    ),
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column that checklists does not have; no
    # alternate soft-delete column or hand-written archive path either. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
    description="ISO9001 compliance checklists against a work order, with templated items.",
    enabled_guard=require_field_tech_enabled,
)
register_resource(FIELD_TECH_CHECKLIST_SPEC)
