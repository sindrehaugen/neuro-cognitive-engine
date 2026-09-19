"""nce.vertical_modules.sites.resources — Resource definitions for C17 Site Master Data.

Phase A Wave A-9:
Registers C12 ResourceSpec for:
  - SITE (sites table)
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

SITE_SPEC = ResourceSpec(
    engine="sites",
    entity="sites",
    node_type="SITE",
    table_name="sites",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="archived",
    filterable_fields=("cadastre_id", "name", "site_type", "archived"),
    searchable_fields=("name", "cadastre_id"),
    writable_fields=(
        "name",
        "cadastre_id",
        "site_type",
        "address",
        "latitude",
        "longitude",
        "altitude",
        "height",
        "footprint_geometry",
        "telemetry_stream",
        "metadata",
        "archived",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "name",
            "cadastre_id",
            "site_type",
            "address",
            "latitude",
            "longitude",
            "altitude",
            "height",
            "footprint_geometry",
            "telemetry_stream",
            "metadata",
            "archived",
            "created_at",
            "updated_at",
        ),
        "external-customer": (
            "id",
            "name",
            "site_type",
            "address",
            "latitude",
            "longitude",
            "created_at",
        ),
    },
    description="C17 master site and building register with cadastre identity, validated address, coordinates, and footprint geometry.",
)
register_resource(SITE_SPEC)
