"""nce.vertical_modules.economy.resources — Resource definitions for Economy.

Lane E Wave E-7:
Registers the C12 ResourceSpec for Economy's owned node type in
node-ownership.json that has a real, tenant-scoped, single-table DDL:
  - POSTING (economy_postings table)

INVOICE, PERIOD, and MARGIN stay exempted. INVOICE and PERIOD are
kg_nodes-only stubs (no dedicated attribute table exists today -- see the
corrected exemptions.py entries; INVOICE's prior exemption text claimed a
non-existent "economy_invoices" table). MARGIN is a per-dimension node
(the margin-trinity pattern): Economy owns only the 'actual' transition, so
it is transition-split like BOM_LINE, not a whole-node Economy resource.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.economy._guard import require_economy_enabled

# 1. POSTING
POSTING_SPEC = ResourceSpec(
    engine="economy",
    entity="postings",
    node_type="POSTING",
    table_name="economy_postings",
    id_field="id",
    version_field=None,
    soft_delete_field=None,
    filterable_fields=("event_id", "event_type", "account", "period_id"),
    searchable_fields=("event_id", "account"),
    writable_fields=(
        "event_id",
        "event_type",
        "line_no",
        "account",
        "amount",
        "period_id",
        "economy_source_id",
        "change_origin",
    ),
    description=(
        "Balanced general-ledger posting lines behind the POSTING node; append-only, "
        "no version/soft-delete field."
    ),
    enabled_guard=require_economy_enabled,
)
register_resource(POSTING_SPEC)
