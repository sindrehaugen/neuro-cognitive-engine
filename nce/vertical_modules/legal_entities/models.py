"""nce.vertical_modules.legal_entities.models — Domain models for C15 Legal-Entity Register.

Phase A Wave A-5:
Defines dataclasses for legal entity records, roles, and group structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class LegalEntityRecord:
    """A registered legal entity in the master legal-entity register."""

    id: UUID
    namespace_id: UUID
    org_nr: str
    name: str
    group_parent_org_nr: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)
    country: str = "NO"
    metadata: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
