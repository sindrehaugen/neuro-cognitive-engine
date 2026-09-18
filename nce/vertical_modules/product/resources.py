"""nce.vertical_modules.product.resources — Resource definitions for Product.

Lane E Wave E-2:
Registers the C12 ResourceSpec for Product's owned node type in node-ownership.json:
  - PRODUCT_SKU (product_catalog table)

product_catalog is a GLOBAL shared parts library (Sindre's ruling, 2026-09-04; see
nce/migrations/064_product_catalog_global.sql): it carries no namespace_id and RLS is
disabled, so ResourceSpec.tenant_scope resolves to "global" via EXPECTED_GLOBAL_TABLES
rather than a hand-set flag. Per-tenant commercial data (list/cost price) lives in the
separate, tenant-scoped product_prices table and is out of scope for this spec.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

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
)
register_resource(PRODUCT_SKU_SPEC)
