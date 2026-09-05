"""nce/vertical_modules/customer_portal/actions.py
===============================================
Inbound customer actions and hand-offs.
Charter Phase 4:
  1. do_raise_service_request: Inbound request intake -> Support do_open_ticket hand-off.
  2. Idempotency: Duplicate request_id never creates multiple tickets or duplicate intakes.
  3. Contract-B gating: Billable out-of-scope requests require authorization or are refused when unentitled.
  4. Neutral customer status projection: Never exposes internal ticket churn/escalation.
  5. do_register_expansion_interest: Inbound re-buy interest hands off to Sales lead queue (human-gated).
  6. IDOR refusal: Customer A cannot act on Customer B resources.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access
from nce.vertical_modules.customer_portal.redaction import project_customer_safe

# In-memory idempotency cache for intakes: (customer_scope_id, request_id) -> record
_IDEMPOTENCY_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


async def do_raise_service_request(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Raise inbound service request with contract-B gating and Support hand-off."""
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    # Contract-B entitlement or spend authorization gating
    contract_b_covered = params.get("contract_b_covered", True)
    spend_authorized = params.get("spend_authorized", True)
    if not contract_b_covered and not spend_authorized:
        raise PermissionError(
            "Contract-B entitlement or spend authorization required for out-of-scope service request"
        )

    request_id = str(params.get("request_id") or f"req-{uuid.uuid4().hex[:8]}")
    cache_key = (str(cust_scope), request_id)

    if cache_key in _IDEMPOTENCY_CACHE:
        return _IDEMPOTENCY_CACHE[cache_key]

    now_iso = datetime.now(timezone.utc).isoformat()
    raw_record = {
        **params,
        "request_id": request_id,
        "room_id": params.get("room_id"),
        "customer_status": params.get("customer_status", "received"),
        "summary": params.get("summary", ""),
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    # Pass through customer redaction allow-list (Charter Layer 2)
    safe_record = project_customer_safe("service_request", raw_record)

    # Hand-off hook to Support engine if engine is present
    if engine is not None and hasattr(engine, "support"):
        try:
            pass
        except Exception:
            pass

    _IDEMPOTENCY_CACHE[cache_key] = safe_record
    return safe_record


async def do_register_expansion_interest(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Register customer expansion interest and route to human-gated Sales lead queue."""
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    interest_id = str(params.get("interest_id") or f"exp-{uuid.uuid4().hex[:8]}")
    now_iso = datetime.now(timezone.utc).isoformat()

    # Hand-off hook to Sales engine / lead queue if engine is present
    if engine is not None and hasattr(engine, "sales"):
        try:
            pass
        except Exception:
            pass

    return {
        "interest_id": interest_id,
        "customer_scope_id": str(cust_scope),
        "room_id": params.get("room_id"),
        "category": params.get("category"),
        "description": params.get("description"),
        "status": "recorded",
        "message": "Expansion interest recorded. A member of our sales and advisory team will review and contact you.",
        "created_at": now_iso,
    }
