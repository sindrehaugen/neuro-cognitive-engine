"""nce.vertical_modules.product.resources — Resource definitions for Product.

Lane E Wave E-2:
Registers the C12 ResourceSpec for Product's owned node type in node-ownership.json:
  - PRODUCT_SKU (product_catalog table)

product_catalog is a GLOBAL shared parts library (Sindre's ruling, 2026-09-04; see
nce/migrations/064_product_catalog_global.sql): it carries no namespace_id and RLS is
disabled, so ResourceSpec.tenant_scope resolves to "global" via EXPECTED_GLOBAL_TABLES
rather than a hand-set flag. Per-tenant commercial data (list/cost price) lives in the
separate, tenant-scoped product_prices table and is out of scope for this spec.

2026-09-21: ``archive`` excluded (``ResourceSpec.excluded_verbs`` reason (4),
spec.py). ``product_catalog`` (schema.sql:1360-1373) has no namespace/owner
column, so a caller-scoped check has nothing to authorize a soft-delete
against a specific row with. Cross-namespace read stays intended (the ruling
above); a caller-scoped mutation verb needs an ownership column this table
does not have.

Wave B-7 (2026-09-20):
  - PACKAGE (product_packages table, migration 101). Genuinely new -- grepped
    first, zero existing package-catalog infrastructure anywhere in nce/.
    Tenant-scoped, unlike PRODUCT_SKU: a package is a tenant's own commercial
    bundling of parts, not a universal shared parts fact. "components" is
    JSONB, a static list -- expanding a package into a live stock reservation
    is do_reserve_kit's job (inventory kitting, IN-2), unchanged by this
    wave; the charter's "expansion = reserve_kit" note describes a future
    wiring point, not something built here.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.product._guard import require_product_enabled

# 1. PRODUCT_SKU
PRODUCT_SKU_SPEC = ResourceSpec(
    engine="product",
    entity="product-skus",
    node_type="PRODUCT_SKU",
    table_name="product_catalog",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_deleted",
    filterable_fields=(
        "gtin",
        "manufacturer",
        "mfr_part_no",
        "product_source_id",
        "lifecycle_status",
    ),
    searchable_fields=("manufacturer", "mfr_part_no", "gtin"),
    writable_fields=(
        "gtin",
        "manufacturer",
        "mfr_part_no",
        "product_source_id",
        "lifecycle_status",
        "etim_specs",
    ),
    description=(
        "Global shared parts library keyed on (manufacturer, mfr_part_no) — one row "
        "per physical part number, shared across every tenant."
    ),
    enabled_guard=require_product_enabled,
    # excluded_verbs reason (4), spec.py: product_catalog has no namespace/owner
    # column, so a caller-scoped soft-delete has nothing to authorize against.
    excluded_verbs=frozenset({"archive"}),
)
register_resource(PRODUCT_SKU_SPEC)


# 2. PACKAGE
PACKAGE_SPEC = ResourceSpec(
    engine="product",
    entity="packages",
    node_type="PACKAGE",
    table_name="product_packages",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("is_archived",),
    searchable_fields=("name", "description"),
    writable_fields=("name", "description", "components"),
    description=(
        "Package catalog definitions -- a named bundle of parts a tenant "
        "sells together, expanded into a stock reservation via the existing "
        "inventory kitting reserve_kit path."
    ),
    enabled_guard=require_product_enabled,
)
register_resource(PACKAGE_SPEC)
