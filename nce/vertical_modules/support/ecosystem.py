"""
nce/vertical_modules/support/ecosystem.py
=========================================
Module 10 (Support Engine) Ecosystem Edges and Executive Morning Brief (#19) Slice:
  - B5 failure_pattern edges -> Product(2) SKU feedback loop.
  - B5 upsell_opportunity edges -> Sales(5) quote/opportunity feedback loop.
  - Operations slice for #19 Morning Brief (M16 Business Insights):
    "drift gråter" signal: at-risk SLA clocks + churn-risk customers + open proactive tickets.

Contract A Boundary Rules:
  - Support owns TICKET; Support asserts assert_owner on TICKET only.
  - Support NEVER asserts ownership over or mutates PRODUCT_SKU or QUOTE/OPPORTUNITY.

Strict Tenant Predicate Discipline (Charter §5.4):
  - Every query against tenant tables (sla_clocks, customer_health, service_tickets, kg_edges)
    carries explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.vertical_modules.support._guard import require_support_enabled
from nce.vertical_modules.support.tickets import (
    TicketNotFoundError,
    _extract_pool,
    _parse_uuid,
    _row_to_dict,
)

log = logging.getLogger("nce.vertical_modules.support.ecosystem")

_NODE_TYPE_TICKET: str = "TICKET"
_SUPPORT_ENGINE: str = "support"


async def do_record_failure_pattern(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record a failure pattern edge from TICKET to PRODUCT_SKU in the Knowledge Graph.

    Closes 'the silence': repeated SKU failures flow back to Product(2) for BOM optimization.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine or asyncpg.Pool instance.
    params:
        - namespace_id: (required) tenant UUID.
        - ticket_id: (required) ticket UUID.
        - product_sku: (required) product SKU string.
        - confidence: (optional) float confidence (default 1.0).
        - pattern_notes: (optional) str notes describing failure pattern.

    Returns
    -------
    dict[str, Any]:
        {"ok": True, "ticket_id": str, "product_sku": str, "edge": str}
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id = _parse_uuid(params.get("ticket_id") or params.get("id"), "ticket_id")

    product_sku = str(params.get("product_sku") or "").strip()
    if not product_sku:
        raise ValueError("product_sku is required and cannot be blank")

    confidence = float(params.get("confidence", 1.0))
    pattern_notes = str(params.get("pattern_notes") or "")

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    ticket_subject = f"TICKET:{ticket_id}"
    product_object = f"PRODUCT_SKU:{product_sku}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Verify ticket existence and tenant isolation
        ticket_row = await conn.fetchrow(
            """
            SELECT id, status, summary
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_id,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_id))

        # 2. Contract A Ownership Guard: Assert Support owns TICKET
        await assert_owner(conn, ns_uuid, _NODE_TYPE_TICKET, _SUPPORT_ENGINE)

        # 3. Insert boundary edge TICKET -[failure_pattern]-> PRODUCT_SKU
        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, 'failure_pattern', $2, $3, $4::uuid, 'agent'
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id)
            DO UPDATE SET confidence = EXCLUDED.confidence
            """,
            ticket_subject,
            product_object,
            confidence,
            ns_uuid,
        )

        # 4. Append failure_pattern event to ticket audit trail
        event = {
            "type": "failure_pattern_recorded",
            "product_sku": product_sku,
            "confidence": confidence,
            "notes": pattern_notes,
            "at": now_dt.isoformat(),
        }
        await conn.execute(
            """
            UPDATE service_tickets
            SET events = events || $1::jsonb,
                updated_at = $2::timestamptz
            WHERE id = $3::uuid AND namespace_id = $4::uuid
            """,
            json.dumps([event]),
            now_dt,
            ticket_id,
            ns_uuid,
        )

    return {
        "ok": True,
        "ticket_id": str(ticket_id),
        "product_sku": product_sku,
        "edge": f"{ticket_subject} -[failure_pattern]-> {product_object}",
    }


async def do_record_upsell_signal(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record an upsell signal edge from TICKET to Sales quote or opportunity.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine or asyncpg.Pool instance.
    params:
        - namespace_id: (required) tenant UUID.
        - ticket_id: (required) ticket UUID.
        - target_type: (optional) 'QUOTE' or 'OPPORTUNITY' (default 'OPPORTUNITY').
        - target_id: (required) quote or opportunity ID string.
        - signal_reason: (optional) str rationale for upsell.
        - confidence: (optional) float confidence (default 1.0).

    Returns
    -------
    dict[str, Any]:
        {"ok": True, "ticket_id": str, "target": str, "edge": str}
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id = _parse_uuid(params.get("ticket_id") or params.get("id"), "ticket_id")

    target_type = str(params.get("target_type") or "OPPORTUNITY").upper()
    if target_type not in ("QUOTE", "OPPORTUNITY"):
        raise ValueError("target_type must be 'QUOTE' or 'OPPORTUNITY'")

    target_id = str(params.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("target_id is required and cannot be blank")

    confidence = float(params.get("confidence", 1.0))
    signal_reason = str(params.get("signal_reason") or "")

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    ticket_subject = f"TICKET:{ticket_id}"
    target_object = f"{target_type}:{target_id}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Verify ticket existence and tenant isolation
        ticket_row = await conn.fetchrow(
            """
            SELECT id, status, summary
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_id,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_id))

        # 2. Contract A Ownership Guard: Assert Support owns TICKET
        await assert_owner(conn, ns_uuid, _NODE_TYPE_TICKET, _SUPPORT_ENGINE)

        # 3. Insert boundary edge TICKET -[upsell_opportunity]-> TARGET
        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, 'upsell_opportunity', $2, $3, $4::uuid, 'agent'
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id)
            DO UPDATE SET confidence = EXCLUDED.confidence
            """,
            ticket_subject,
            target_object,
            confidence,
            ns_uuid,
        )

        # 4. Append upsell event to ticket audit trail
        event = {
            "type": "upsell_signal_recorded",
            "target": target_object,
            "confidence": confidence,
            "reason": signal_reason,
            "at": now_dt.isoformat(),
        }
        await conn.execute(
            """
            UPDATE service_tickets
            SET events = events || $1::jsonb,
                updated_at = $2::timestamptz
            WHERE id = $3::uuid AND namespace_id = $4::uuid
            """,
            json.dumps([event]),
            now_dt,
            ticket_id,
            ns_uuid,
        )

    return {
        "ok": True,
        "ticket_id": str(ticket_id),
        "target": target_object,
        "edge": f"{ticket_subject} -[upsell_opportunity]-> {target_object}",
    }


async def do_support_at_risk_aggregate(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Expose the 'drift gråter' operations slice for Executive Morning Brief (#19).

    Cross-engine intelligence consumed by Module 16 (Business Insights):
      - at-risk SLA clocks (breached or within approaching breach window)
      - churn-risk customers (health score critical/high risk)
      - open proactive tickets (telemetry and health driven)

    Parameters
    ----------
    engine_or_pool:
        NCEEngine or asyncpg.Pool instance.
    params:
        - namespace_id: (required) tenant UUID.
        - lookback_days: (optional) integer lookback window.

    Returns
    -------
    dict[str, Any]:
        Structured operations slice with counts and detailed at-risk entities.
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    await require_support_enabled(pool, ns_uuid)

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    breach_lookahead = now_dt + datetime.timedelta(hours=4)

    sla_at_risk: list[dict[str, Any]] = []
    churn_customers: list[dict[str, Any]] = []
    proactive_tickets: list[dict[str, Any]] = []

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. At-risk SLA clocks
        sla_rows = await conn.fetch(
            """
            SELECT sc.ticket_id, sc.sla_profile, sc.resolution_due, sc.breached, sc.breach_type,
                   st.summary, st.priority, st.status
            FROM sla_clocks sc
            JOIN service_tickets st ON st.id = sc.ticket_id AND st.namespace_id = sc.namespace_id
            WHERE sc.namespace_id = $1::uuid
              AND st.status IN ('open', 'in_progress', 'waiting_parts')
              AND (sc.breached = TRUE OR sc.resolution_due <= $2::timestamptz)
            ORDER BY sc.resolution_due ASC
            LIMIT 50
            """,
            ns_uuid,
            breach_lookahead,
        )
        for r in sla_rows:
            sla_at_risk.append(_row_to_dict(r))

        # 2. Churn-risk customers
        churn_rows = await conn.fetch(
            """
            SELECT customer_id, score, trend, churn_risk, drivers, last_touchpoint_at
            FROM customer_health
            WHERE namespace_id = $1::uuid
              AND churn_risk IN ('high', 'critical')
            ORDER BY score ASC
            LIMIT 50
            """,
            ns_uuid,
        )
        for r in churn_rows:
            churn_customers.append(_row_to_dict(r))

        # 3. Active proactive tickets
        proactive_rows = await conn.fetch(
            """
            SELECT id, asset_id, room_id, customer_id, summary, priority, status, created_at
            FROM service_tickets
            WHERE namespace_id = $1::uuid
              AND status IN ('open', 'in_progress', 'waiting_parts')
              AND change_origin IN ('proactive_telemetry', 'proactive_health')
            ORDER BY created_at DESC
            LIMIT 50
            """,
            ns_uuid,
        )
        for r in proactive_rows:
            proactive_tickets.append(_row_to_dict(r))

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "computed_at": now_dt.isoformat(),
        "operations_slice": {
            "sla_at_risk_count": len(sla_at_risk),
            "sla_at_risk": sla_at_risk,
            "churn_risk_count": len(churn_customers),
            "churn_risk_customers": churn_customers,
            "proactive_tickets_count": len(proactive_tickets),
            "proactive_tickets": proactive_tickets,
        },
    }


# Standard alias matching the vertical engine morning brief naming pattern
get_support_morning_brief_slice = do_support_at_risk_aggregate
