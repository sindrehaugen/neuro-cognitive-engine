"""nce.vertical_modules.documents.models — Domain models for C14 Document Register.

Phase A Wave A-4:
Defines dataclasses for document records, entity links, and expiring share tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class DocumentRecord:
    """A registered document in the document register."""

    id: UUID
    namespace_id: UUID
    title: str
    document_ref: str
    source_kind: str = "sharepoint"
    document_kind: str = "other"
    file_name: str | None = None
    mime_type: str | None = None
    file_size_bytes: int | None = None
    sha256: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class DocumentLinkRecord:
    """A polymorphic link connecting a document to a business entity."""

    id: UUID
    namespace_id: UUID
    document_id: UUID
    entity_type: str
    entity_id: str
    relation: str = "about"
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class DocumentShareRecord:
    """An expiring, revocable share token granting access to a document."""

    id: UUID
    share_id: str
    namespace_id: UUID
    document_id: UUID
    customer_scope_id: UUID | None = None
    granted_by: str = "system"
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
