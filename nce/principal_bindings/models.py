"""
nce/principal_bindings/models.py

C16 Principal Mapping (Wave A-6)
Models for principal identity resolution across multi-tenant boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class PrincipalBinding:
    """Multi-tenant binding record mapping caller principal_id to domain identities and roles."""

    id: UUID
    namespace_id: UUID
    principal_id: str
    tier: str = "employee"
    employee_id: str | None = None
    customer_id: str | None = None
    contractor_id: str | None = None
    roles: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class PrincipalContext:
    """Resolved context for the current calling principal.

    Contract:
    - If bound, carries the resolved domain identities (employee_id, customer_id, contractor_id)
      and roles from the principal_bindings table.
    - If unbound (or missing context), is_bound=False and all actor IDs are None and roles=().
    - Downstream "mine" filters MUST yield empty results for unbound principals, never another
      person's rows.
    """

    principal_id: str
    tier: str = "employee"
    employee_id: str | None = None
    customer_id: str | None = None
    contractor_id: str | None = None
    roles: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    namespace_id: UUID | None = None
    is_bound: bool = False

    @property
    def is_employee(self) -> bool:
        return self.tier == "employee"

    @property
    def is_customer(self) -> bool:
        return self.tier in ("customer", "external-customer")

    @property
    def is_contractor(self) -> bool:
        return self.tier == "contractor"

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_api_dict(self) -> dict[str, Any]:
        """Format for GET /api/me/context per C16 specification."""
        d: dict[str, Any] = {
            "principal_id": self.principal_id,
            "tier": self.tier,
            "employee_id": self.employee_id,
            "customer_id": self.customer_id,
            "contractor_id": self.contractor_id,
            "roles": list(self.roles),
            "is_bound": self.is_bound,
        }
        return d
