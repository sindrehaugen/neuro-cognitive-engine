"""nce.vertical_modules.vendors.resources — Resource definitions for Vendors.

Lane E Wave E-5, corrected 2026-09-19: this module registers NOTHING.

It is kept as the conventional discovery point -- ``load_all_engine_resources()``
imports ``nce.vertical_modules.<engine>.resources`` for every engine by
convention -- so that the reasoning below is found by whoever next asks why
Vendors has no resource surface.
  - CONTRACTOR: NOT declared. Exempted in resource_surface/exemptions.py as
    UNREACHABLE -- contractor_profiles' sole RLS policy requires the
    nce.external_scope_id GUC, which admin_app.py:59 contractually never
    sets, so a generated surface would return zero rows and fail every
    write. See that exemption for the full evidence.

VENDOR and CERT stay exempted in resource_surface/exemptions.py: their real
attribute data lives in MongoDB (addressed from kg_nodes by payload_ref), not
Postgres, so neither EXPECTED_TENANT_RLS_TABLES nor EXPECTED_GLOBAL_TABLES
applies to them and no table_name can be declared. VENDORS_CERT stays exempted
as a dead registry row with zero code references anywhere in the tree.

contractor_profiles also has no `id` column -- its primary key is the composite
(contractor_id, namespace_id) -- which is recorded here for whoever revisits
this if the table ever becomes reachable from an employee-side path.
"""

from __future__ import annotations

# Intentionally no imports from nce.resource_surface and no register_resource()
# call: every Vendors node type is exempted. Re-adding a spec here requires
# resolving the RLS reachability problem documented above first.

