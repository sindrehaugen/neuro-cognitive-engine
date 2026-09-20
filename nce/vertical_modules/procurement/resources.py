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
#
# excluded_verbs={"upsert", "archive"} -- two verbs, two unrelated reasons,
# stated separately per ResourceSpec.excluded_verbs's own docstring:
#
# "upsert" -- reason (3): a governed writer already owns the write path.
# upsert_po_line_node (po_line.py:110, assert_owner call at :137) and
# update_po_line_status (po_line.py:266, assert_owner call at :319) are both
# real, already-shipped writers that compute transition=f"status:{...}"
# explicitly and call assert_owner directly. The generic route's create/
# patch/bulk never do this: assert_owner is only reachable inside
# rest.py/mcp.py's `if is_graph:` branches (rest.py:747/1043, mcp.py:563),
# and is_graph = spec.tenant_scope == "graph" is structurally False for
# every table-backed spec (spec.py's tenant_scope derivation), PO_LINE_SPEC
# included. So enabling the generic upsert here would not add a second
# equally-guarded writer -- it would add one with no ownership check at
# all, bypassing both governed writers' per-transition discipline.
#
# "archive" -- fits none of the three documented reasons, stated plainly
# rather than dressed as one. soft_delete_field=None falls back to
# "is_archived" (rest.py:524/1174/1270), but procurement_po_lines has no
# such column anywhere in schema.sql -- archive/restore would raise
# asyncpg.UndefinedColumnError, caught by the generic handler's own
# broad exception clause and returned as a 500. Measured as 1 of 25
# registered specs with this exact shape (soft_delete_field=None,
# table-backed, archive not excluded, target column absent) -- see
# _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
# Whether that pattern warrants a fourth excluded_verbs reason, or a
# schema migration adding the column across all 25, is undecided; this
# spec is excluded here only to stop it 500ing today, not as a precedent.
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
    # writable_fields cleared to () because "upsert" is excluded: fail-safe,
    # not cosmetic -- see ResourceSpec.excluded_verbs's own docstring. A
    # populated list has no reader today, but if a future change un-excludes
    # upsert, a stale populated list would make every field immediately
    # writable with no review; an empty one forces someone to deliberately
    # list fields instead.
    writable_fields=(),
    description=(
        "Purchase-order lines, self-transitioning through draft/ordered/received/"
        "cancelled status, all owned by Procurement."
    ),
    excluded_verbs=frozenset({"upsert", "archive"}),
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
