"""nce.vertical_modules.procurement.resources — Resource definitions for Procurement.

Lane E Wave E-3:
Registers the C12 ResourceSpec for Procurement's owned node type in
node-ownership.json that has a real, tenant-scoped, single-table DDL:
  - PO_LINE (procurement_po_lines table)

Wave B-15 (2026-09-20):
  - DEAL_REGISTRATION (procurement_deal_registrations table, migration 100)

Premise check before building DEAL_REGISTRATION: the charter's EXISTS column
cites "Procurement rebate model", but procurement_kickback_tiers -- the table
frontier.py's rebate forecasting reads from -- does not exist anywhere in
schema.sql or migrations; that code already degrades to empty tiers via
to_regclass() when it's absent. The charter's consumer column ("Agreements
compliance audit reads it") is equally aspirational: do_run_compliance_audit
only reads agreement_review_queue today. Neither blocks this resource --
DEAL_REGISTRATION is a self-contained record -- but this wave builds ONLY the
C12 surface below, not a consumer nothing calls for yet.

PO, PROCUREMENT_MATCH, and BOM_LINE stay exempted in resource_surface/exemptions.py:
PO and PROCUREMENT_MATCH are kg_nodes-only stubs with no attribute table, and BOM_LINE
is split by transition across five engines (system_design, sales, procurement,
inventory, field_tech) rather than owned outright by any one of them.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

# 1. PO_LINE
PO_LINE_SPEC = ResourceSpec(
    engine="procurement",
    entity="po-lines",
    node_type="PO_LINE",
    table_name="procurement_po_lines",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("po_number", "line_ref", "project_id", "status"),
    searchable_fields=("po_number", "line_ref", "artnr", "description"),
    writable_fields=(
        "po_number",
        "line_ref",
        "project_id",
        "bom_line_label",
        "artnr",
        "description",
        "quantity",
        "unit_price",
        "line_total",
        "currency",
        "status",
    ),
    description=(
        "Purchase-order lines, self-transitioning through draft/ordered/received/"
        "cancelled status, all owned by Procurement."
    ),
)
register_resource(PO_LINE_SPEC)


# 2. DEAL_REGISTRATION
DEAL_REGISTRATION_SPEC = ResourceSpec(
    engine="procurement",
    entity="deal-registrations",
    node_type="DEAL_REGISTRATION",
    table_name="procurement_deal_registrations",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("supplier", "status"),
    searchable_fields=("deal_name", "supplier"),
    writable_fields=(
        "supplier",
        "deal_name",
        "status",
        "estimated_value",
        "registered_by",
        "valid_from",
        "valid_to",
        "notes",
    ),
    description=(
        "Supplier deal registrations -- protects pricing/terms for a specific "
        "opportunity with a named supplier, owned by Procurement."
    ),
)
register_resource(DEAL_REGISTRATION_SPEC)
