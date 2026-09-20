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
    # archive wave (2026-09-20): soft_delete_field=None falls back to a
    # literal "is_archived" column (rest.py:524/1174/1270) that "assets"
    # does not have -- no alternate soft-delete-shaped column either
    # (lifecycle_state is a free-form non-blank TEXT field, no enumerated
    # CHECK, not an archived/inactive state) and no hand-written archive
    # path anywhere in this engine. See
    # _internal/work-docs/mlv16-orchestration/ARCHIVE_COLUMN_SWEEP.md.
    excluded_verbs=frozenset({"archive"}),
    description="Physical installed equipment register with lifecycle state, serials, and room locations.",
)
register_resource(ASSET_SPEC)

ASSET_SPECS = [ASSET_SPEC]
