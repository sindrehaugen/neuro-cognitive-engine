"""nce.vertical_modules.vendors.resources — Resource definitions for Vendors.

Lane E Wave E-5:
Registers the C12 ResourceSpec for Vendors' owned node type in
node-ownership.json that has a real, tenant-scoped, single-table DDL:
  - CONTRACTOR (contractor_profiles table)

VENDOR and CERT stay exempted in resource_surface/exemptions.py: their real
attribute data lives in MongoDB (addressed from kg_nodes by payload_ref), not
Postgres, so neither EXPECTED_TENANT_RLS_TABLES nor EXPECTED_GLOBAL_TABLES
applies to them and no table_name can be declared. VENDORS_CERT stays exempted
as a dead registry row with zero code references anywhere in the tree.

contractor_profiles has no `id` column -- its primary key is the composite
(contractor_id, namespace_id) -- so id_field is declared explicitly rather
than left at the ResourceSpec default.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

# 1. CONTRACTOR
CONTRACTOR_SPEC = ResourceSpec(
    engine="vendors",
    entity="contractors",
    node_type="CONTRACTOR",
    table_name="contractor_profiles",
    id_field="contractor_id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("partner_scope_id",),
    searchable_fields=("contractor_id",),
    writable_fields=(
        "partner_scope_id",
        "profile",
        "rates",
        "skills",
        "availability",
        "performance_score",
    ),
    description=(
        "Contractor/partner profiles: rates, skills, availability, and rolling "
        "performance score, scoped to a partner_scope_id."
    ),
)
register_resource(CONTRACTOR_SPEC)
