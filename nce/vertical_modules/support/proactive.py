"""
nce/vertical_modules/support/proactive.py
========================================
Proactive Telemetry Ticket Creation for Module 10 (Support Engine):
  - Consumes asset telemetry degradation signals from Assets(9).
  - Authors proactive ServiceTickets (origin: 'proactive_telemetry').
  - Connects TICKET -[about]-> ASSET boundary edge in kg_edges.
  - Enforces Contract A: Support owns TICKET; Support NEVER owns or mutates ASSET.
  - Enforces Idempotency: Avoids opening duplicate proactive tickets for an asset
    while a proactive ticket is already active.

Strict Tenant Predicate Discipline (Charter §5.4):
All SQL statements carry explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.vertical_modules.support.tickets import (
    _extract_pool,
    _parse_uuid,
    do_open_ticket,
)

log = logging.getLogger("nce.vertical_modules.support.proactive")

_NODE_TYPE_TICKET: str = "TICKET"
_SUPPORT_ENGINE: str = "support"


async def do_open_proactive_telemetry_ticket(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Open a proactive service ticket triggered by asset telemetry degradation.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine or asyncpg.Pool instance.
    params:
        - namespace_id: (required) tenant UUID.
        - asset_id: (required) degraded asset UUID.
        - summary: (optional) summary description.
        - priority: (optional) default 'high'.
        - metric_name: (optional) name of degraded metric.
        - telemetry_data: (optional) dict of telemetry readings / fault info.
        - room_id: (optional) functional location / room ID string.
        - customer_id: (optional) customer ID string.
        - sla_profile: (optional) SLA profile name (default 'standard').

    Returns
    -------
    dict[str, Any]:
        {"ok": True, "ticket_id": str, "ticket": dict, "edge": str, "proactive": True}
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    asset_uuid = _parse_uuid(params.get("asset_id"), "asset_id")

    metric_name = params.get("metric_name")
    telemetry_data = params.get("telemetry_data") or {}

    summary = params.get("summary")
    if not summary:
        if metric_name:
            summary = f"Proactive telemetry alert: asset {asset_uuid} metric {metric_name} degraded"
        else:
            summary = f"Proactive telemetry alert: asset {asset_uuid} health degraded"

    priority = str(params.get("priority") or "high").lower()
    sla_profile = str(params.get("sla_profile") or "standard")
    room_id = params.get("room_id")
    customer_id = params.get("customer_id")

    ai_diag = params.get("ai_diagnosis") or {}
    if telemetry_data and "telemetry_metrics" not in ai_diag:
        ai_diag["telemetry_metrics"] = telemetry_data

    ticket_subject_prefix = "TICKET:"
    asset_object = f"ASSET:{asset_uuid}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Idempotency Check: Is there already an active proactive ticket for this asset?
        existing = await conn.fetchrow(
            """
            SELECT id, status, summary, created_at
            FROM service_tickets
            WHERE asset_id = $1::uuid
              AND namespace_id = $2::uuid
              AND status IN ('open', 'in_progress', 'waiting_parts')
              AND change_origin = 'proactive_telemetry'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            asset_uuid,
            ns_uuid,
        )
        if existing is not None:
            existing_ticket_id = str(existing["id"])
            log.info(
                "Proactive ticket already active for asset_id=%s -> ticket_id=%s",
                asset_uuid,
                existing_ticket_id,
            )
            return {
                "ok": True,
                "ticket_id": existing_ticket_id,
                "idempotent_replay": True,
                "status": existing["status"],
                "summary": existing["summary"],
                "edge": f"{ticket_subject_prefix}{existing_ticket_id} -[about]-> {asset_object}",
            }

        # 2. Contract A Ownership Guard: Assert Support owns TICKET
        await assert_owner(conn, ns_uuid, _NODE_TYPE_TICKET, _SUPPORT_ENGINE)

    # 3. Create native ServiceTicket
    open_params = {
        "namespace_id": str(ns_uuid),
        "asset_id": str(asset_uuid),
        "summary": summary,
        "priority": priority,
        "change_origin": "proactive_telemetry",
        "source": "nce",
        "sla_profile": sla_profile,
        "ai_diagnosis": ai_diag,
    }
    if room_id:
        open_params["room_id"] = str(room_id)
    if customer_id:
        open_params["customer_id"] = str(customer_id)

    open_res = await do_open_ticket(pool, open_params)
    ticket_id = open_res["ticket"]["id"]
    ticket_subject = f"TICKET:{ticket_id}"

    # 4. Insert boundary edge TICKET -[about]-> ASSET into kg_edges
    async with scoped_pg_session(pool, ns_uuid) as conn:
        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, 'about', $2, 1.0, $3::uuid, 'proactive_telemetry'
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            ticket_subject,
            asset_object,
            ns_uuid,
        )

    return {
        "ok": True,
        "ticket_id": str(ticket_id),
        "ticket": open_res["ticket"],
        "sla_clock": open_res.get("sla_clock"),
        "edge": f"{ticket_subject} -[about]-> {asset_object}",
        "proactive": True,
    }
