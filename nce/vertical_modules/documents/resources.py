"""nce.vertical_modules.documents.resources — Resource definitions for C14 Document Register.

Phase A Wave A-4:
Registers C12 ResourceSpec for:
  - DOCUMENT (documents table)
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

DOCUMENT_SPEC = ResourceSpec(
    engine="documents",
    entity="documents",
    node_type="DOCUMENT",
    table_name="documents",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="archived",
    filterable_fields=("document_kind", "source_kind", "mime_type", "archived"),
    searchable_fields=("title", "file_name", "document_ref"),
    writable_fields=(
        "title",
        "document_ref",
        "source_kind",
        "document_kind",
        "file_name",
        "mime_type",
        "file_size_bytes",
        "sha256",
        "tags",
        "metadata",
        "archived",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "title",
            "document_ref",
            "source_kind",
            "document_kind",
            "file_name",
            "mime_type",
            "file_size_bytes",
            "tags",
            "metadata",
            "archived",
            "created_at",
            "updated_at",
        ),
        "external-customer": (
            "id",
            "title",
            "document_kind",
            "file_name",
            "mime_type",
            "created_at",
        ),
    },
    description="C14 master document register for cross-engine document metadata and storage references.",
)
register_resource(DOCUMENT_SPEC)
