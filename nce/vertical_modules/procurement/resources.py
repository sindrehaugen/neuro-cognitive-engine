"""nce.vertical_modules.procurement.resources — Resource definitions for Procurement.

Lane E Wave E-3:
Registers the C12 ResourceSpec for Procurement's owned node type in
node-ownership.json that has a real, tenant-scoped, single-table DDL:
  - PO_LINE (procurement_po_lines table)

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
