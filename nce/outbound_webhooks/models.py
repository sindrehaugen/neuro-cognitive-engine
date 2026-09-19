"""
nce/outbound_webhooks/models.py

C4 Outbound Webhooks (Wave A-7)
Data models for outbound webhook subscriptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class OutboundWebhook:
    """Outbound webhook subscription record."""

    id: UUID
    namespace_id: UUID
    url: str
    secret: str
    selectors: tuple[str, ...] = ()
    is_active: bool = True
    description: str = ""
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Serialize to dictionary suitable for JSON responses."""
        data: dict[str, Any] = {
            "id": str(self.id),
            "namespace_id": str(self.namespace_id),
            "url": self.url,
            "selectors": list(self.selectors),
            "is_active": self.is_active,
            "description": self.description,
            "created_at": (
                self.created_at.isoformat()
                if isinstance(self.created_at, datetime)
                else (str(self.created_at) if self.created_at else None)
            ),
            "updated_at": (
                self.updated_at.isoformat()
                if isinstance(self.updated_at, datetime)
                else (str(self.updated_at) if self.updated_at else None)
            ),
        }
        if include_secret:
            data["secret"] = self.secret
        return data
