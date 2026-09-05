"""nce/vertical_modules/customer_portal/invoices.py
================================================
Customer invoice read projections.
Charter Phase 3: Invoices from Economy, strictly stripping internal margin, our_cost, and rebate.

Enforces:
  1. Customer scope boundary (IDOR denial).
  2. Graceful degradation when Economy engine data is empty.
  3. Strict allow-list redaction (customer-redaction.json).
"""

from __future__ import annotations

from typing import Any

from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access
from nce.vertical_modules.customer_portal.redaction import project_customer_safe


async def do_list_invoices(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List customer invoices with commercial margin and cost stripped."""
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    raw_invoices = params.get("invoices", [])

    projected_invoices = [project_customer_safe("invoices", inv) for inv in raw_invoices]

    return {
        "customer_scope_id": str(cust_scope),
        "total_invoices": len(projected_invoices),
        "invoices": projected_invoices,
    }
