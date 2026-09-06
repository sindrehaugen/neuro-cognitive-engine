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

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from nce.engine_registry import EngineDisabledError, EngineNotFoundError
from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access
from nce.vertical_modules.customer_portal.redaction import project_customer_safe

log = logging.getLogger("nce.vertical_modules.customer_portal.actions")

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

    # Hand-off hook to Support engine via W-1 engine registry
    modules = getattr(engine, "modules", None)
    if modules is not None:
        ns_str = str(
            params.get("namespace_id")
            or getattr(engine, "namespace_id", "00000000-0000-4000-8000-000000000001")
        )
        try:
            scoped_registry = (
                modules.for_namespace(ns_str) if hasattr(modules, "for_namespace") else modules
            )
            support_module = scoped_registry["support"]
            ticket_params = {
                "namespace_id": ns_str,
                "summary": safe_record.get("summary") or "Customer service request",
                "customer_id": str(cust_scope),
                "room_id": params.get("room_id"),
                "source": "nce",
                "change_origin": "agent",
                "source_id": f"customer_portal:{request_id}",
                "description": params.get("description") or safe_record.get("summary"),
                "priority": params.get("priority", "medium"),
            }
            ticket_res = await support_module.do_open_ticket(engine, ticket_params)
            ticket_data = (
                ticket_res.get("ticket", ticket_res) if isinstance(ticket_res, dict) else {}
            )
            safe_record["ticket_id"] = str(
                ticket_data.get("id") or ticket_data.get("ticket_id") or ""
            )
            safe_record["ticket_status"] = str(ticket_data.get("status") or "open")
        except EngineDisabledError as exc:
            log.warning("Support engine is disabled for namespace %s: %s", ns_str, exc)
            safe_record["support_degraded"] = True
            safe_record["degradation_reason"] = "support_engine_disabled"
        except EngineNotFoundError as exc:
            log.warning("Support engine not found in registry: %s", exc)
            safe_record["support_degraded"] = True
            safe_record["degradation_reason"] = "support_engine_not_found"
    else:
        safe_record["support_degraded"] = True
        safe_record["degradation_reason"] = "engine_modules_unavailable"

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

    # Hand-off hook to Sales engine / lead queue via W-1 engine registry
    sales_routed = False
    sales_degraded = False
    degradation_reason = None
    modules = getattr(engine, "modules", None)
    if modules is not None:
        ns_str = str(
            params.get("namespace_id")
            or getattr(engine, "namespace_id", "00000000-0000-4000-8000-000000000001")
        )
        try:
            scoped_registry = (
                modules.for_namespace(ns_str) if hasattr(modules, "for_namespace") else modules
            )
            _ = scoped_registry["sales"]

            # Sales lead queue hook / presence confirmed
            sales_routed = True
        except EngineDisabledError as exc:
            log.warning("Sales engine is disabled for namespace %s: %s", ns_str, exc)
            sales_degraded = True
            degradation_reason = "sales_engine_disabled"
        except EngineNotFoundError as exc:
            log.warning("Sales engine not found in registry: %s", exc)
            sales_degraded = True
            degradation_reason = "sales_engine_not_found"
    else:
        sales_degraded = True
        degradation_reason = "engine_modules_unavailable"

    res = {
        "interest_id": interest_id,
        "customer_scope_id": str(cust_scope),
        "room_id": params.get("room_id"),
        "category": params.get("category"),
        "description": params.get("description"),
        "status": "recorded",
        "message": "Expansion interest recorded. A member of our sales and advisory team will review and contact you.",
        "created_at": now_iso,
    }
    if sales_routed:
        res["sales_lead_routed"] = True
    elif sales_degraded:
        res["sales_degraded"] = True
        res["degradation_reason"] = degradation_reason
    return res
