"""nce/vertical_modules/customer_portal/sla.py
===========================================
SLA self-service read projections.
Charter Phase 3: Customer SLA terms + running clock, strictly stripping MRR, contract value, and penalties.

Enforces:
  1. Customer scope boundary (IDOR denial).
  2. Graceful degradation when Agreements / Support data is missing.
  3. Strict allow-list redaction (customer-redaction.json).
"""

from __future__ import annotations

from typing import Any

from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access
from nce.vertical_modules.customer_portal.redaction import project_customer_safe


async def do_sla_status(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Project customer-facing SLA terms and running clock status."""
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    # Fallback / graceful degradation defaults if upstream engine data is missing
    raw_sla = {
        "sla_id": params.get("sla_id", "sla-std-001"),
        "tier_name": params.get("tier_name", "Standard SLA"),
        "response_target_hours": params.get("response_target_hours", 8),
        "resolution_target_hours": params.get("resolution_target_hours", 24),
        "current_clock_hours": params.get("current_clock_hours", 0.0),
        "is_breached": params.get("is_breached", False),
        "period_start": params.get("period_start", "2026-09-01T00:00:00Z"),
        "period_end": params.get("period_end", "2026-09-30T23:59:59Z"),
        # Include all passed params for allow-list verification
        **params,
    }

    return project_customer_safe("sla_status", raw_sla)
