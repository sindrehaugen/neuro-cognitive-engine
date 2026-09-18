"""nce.vertical_modules.legal_entities.resources — Resource definitions for C15 Legal-Entity Register.

Phase A Wave A-5:
Registers C12 ResourceSpec for:
  - LEGAL_ENTITY (legal_entities table)
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

LEGAL_ENTITY_SPEC = ResourceSpec(
    engine="legal_entities",
    entity="legal_entities",
    node_type="LEGAL_ENTITY",
    table_name="legal_entities",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="archived",
    filterable_fields=("org_nr", "name", "country", "group_parent_org_nr", "archived"),
    searchable_fields=("name", "org_nr", "group_parent_org_nr"),
    writable_fields=(
        "org_nr",
        "name",
        "group_parent_org_nr",
        "roles",
        "country",
        "metadata",
        "archived",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "org_nr",
            "name",
            "group_parent_org_nr",
            "roles",
            "country",
            "metadata",
            "archived",
            "created_at",
            "updated_at",
        ),
        "external-customer": (
            "id",
            "org_nr",
            "name",
            "country",
            "created_at",
        ),
    },
    description="C15 master legal-entity register for company identities, roles, and group structures.",
)
register_resource(LEGAL_ENTITY_SPEC)
