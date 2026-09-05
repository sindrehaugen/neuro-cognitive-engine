"""nce/vertical_modules/customer_portal/advisor.py
===============================================
Sandboxed Customer Advisor Surface (Charter Layer 4).

AI assistant providing:
  - Room-status narrative (Domino's tracker & BOM_LINE readiness translated into plain language).
  - Post-handover self-service guidance (how to raise service requests, find documentation).
  - Prompt-injection sandboxing:
      * Assistant CANNOT be talked into another customer's data (structural isolation).
      * Churn risk, health scores, supplier terms, margins, and internal slip NEVER shown.
      * Denial by default on IDOR cross-customer scope attempts.
"""

from __future__ import annotations

import re
from typing import Any

from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access

# Forbidden internal keywords that must never be echoed or accepted in generation
_FORBIDDEN_INTERNAL_PATTERNS = re.compile(
    r"\b(churn[_\s-]?risk|health[_\s-]?score|margin|profit|our[_\s-]?cost|internal[_\s-]?notes|"
    r"supplier[_\s-]?terms|internal[_\s-]?priority|escalation[_\s-]?level|p1_critical)\b",
    re.IGNORECASE,
)

_INJECTION_PATTERNS = re.compile(
    r"\b(system\s+override|ignore\s+(all\s+)?(previous|prior)\s+(rules|instructions)|"
    r"you\s+are\s+in\s+debug|switch\s+scope|reveal\s+internal|print\s+all)\b",
    re.IGNORECASE,
)


async def do_advisor_answer(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Generate customer-safe advisor response under strict isolation and prompt sandboxing."""
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    raw_query = str(params.get("query", "")).strip()
    room_id = params.get("room_id")

    # Layer 4 Defense: Check for adversarial prompt injection or probe for internal metrics
    has_injection = bool(_INJECTION_PATTERNS.search(raw_query))
    probes_forbidden = bool(_FORBIDDEN_INTERNAL_PATTERNS.search(raw_query))

    if has_injection or probes_forbidden:
        return {
            "customer_scope_id": str(cust_scope),
            "query": raw_query,
            "room_id": room_id,
            "narrative_status": "guidance",
            "answer": (
                "I cannot fulfill this request. As your customer portal assistant, I can only assist "
                "with authorized information for your organization and cannot disclose internal metrics "
                "or other customers' data. Please let me know if I can help with your room status, "
                "installed equipment, or submitting a service request."
            ),
        }

    # Guidance on service requests
    query_lower = raw_query.lower()
    if any(
        kw in query_lower
        for kw in (
            "service request",
            "report",
            "broken",
            "ticket",
            "support",
            "microphone",
            "repair",
            "issue",
        )
    ):
        return {
            "customer_scope_id": str(cust_scope),
            "query": raw_query,
            "room_id": room_id,
            "narrative_status": "guidance",
            "answer": (
                "To report an issue or request maintenance, you can submit a service request directly "
                "through the customer portal (/api/portal/service-requests). Our support team will review "
                "the ticket and coordinate any required service."
            ),
        }

    # Room-status narrative
    room_data = params.get("room_data")
    if room_data and isinstance(room_data, dict):
        room_name = room_data.get("room_name") or room_id or "Your room"
        percent = room_data.get("percent_ready", 0)
        stage = room_data.get("stage", "in_progress")
        summary = room_data.get("summary", "")
        narrative_status = "ready" if percent >= 100 else "in_progress"
        narrative_parts = [f"{room_name} is currently at the {stage} stage ({percent}% ready)."]
        if summary:
            narrative_parts.append(summary)
        return {
            "customer_scope_id": str(cust_scope),
            "query": raw_query,
            "room_id": room_id,
            "narrative_status": narrative_status,
            "answer": " ".join(narrative_parts),
        }

    if room_id:
        return {
            "customer_scope_id": str(cust_scope),
            "query": raw_query,
            "room_id": room_id,
            "narrative_status": "in_progress",
            "answer": f"Your room ({room_id}) is currently progressing through deployment. Check the room tracker for live stage updates.",
        }

    # Default friendly greeting / capabilities
    return {
        "customer_scope_id": str(cust_scope),
        "query": raw_query,
        "room_id": room_id,
        "narrative_status": "guidance",
        "answer": (
            "Hello! I am your customer portal assistant. You can ask me about the installation "
            "progress of your rooms, your covered equipment, or how to submit a service request."
        ),
    }
