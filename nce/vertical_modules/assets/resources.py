"""nce.vertical_modules.assets.resources — Resource definitions for Assets Engine.

Phase D Wave D-1:
Registers C12 ResourceSpec for Assets' owned node type in node-ownership.json:
  - ASSET (assets table)
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

ASSET_SPEC = ResourceSpec(
    engine="assets",
    entity="assets",
    node_type="ASSET",
    table_name="assets",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=(
        "bom_line_id",
        "serial",
        "functional_location_id",
        "lifecycle_state",
        "is_shell",
        "product_id",
    ),
    searchable_fields=("serial", "bom_line_id", "functional_location_id"),
    writable_fields=(
        "bom_line_id",
        "serial",
        "functional_location_id",
        "lifecycle_state",
        "change_origin",
        "is_shell",
        "product_id",
        "product_sku",
    ),
    tier_allowlists={
        "external-customer": (
            "id",
            "serial",
            "functional_location_id",
            "lifecycle_state",
            "is_shell",
            "created_at",
            "updated_at",
        ),
        "contractor": (
            "id",
            "bom_line_id",
            "serial",
            "functional_location_id",
            "lifecycle_state",
            "is_shell",
            "created_at",
            "updated_at",
        ),
    },
    description="Physical installed equipment register with lifecycle state, serials, and room locations.",
)
register_resource(ASSET_SPEC)

ASSET_SPECS = [ASSET_SPEC]
