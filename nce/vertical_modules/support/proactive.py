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
from uuid import UUID

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


async def _find_upstream_root_cause_ticket(
    conn: Any,
    ns_uuid: UUID,
    asset_uuid: UUID,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Find active proactive ticket on an upstream asset connected to asset_uuid (I-VERT-01)."""
    upstream_ids = params.get("upstream_asset_ids")
    if upstream_ids:
        parsed_upstream_uuids = [_parse_uuid(u, "upstream_asset_ids") for u in upstream_ids]
        row = await conn.fetchrow(
            """
            SELECT id, asset_id, status, summary
            FROM service_tickets
            WHERE asset_id = ANY($1::uuid[])
              AND namespace_id = $2::uuid
              AND status IN ('open', 'in_progress', 'waiting_parts')
              AND (change_origin = 'proactive_telemetry' OR (change_origin = 'agent' AND source = 'nce'))
            ORDER BY created_at DESC
            LIMIT 1
            """,
            parsed_upstream_uuids,
            ns_uuid,
        )
        if row is not None and hasattr(row, "__getitem__"):
            try:
                return {
                    "id": row["id"],
                    "asset_id": row["asset_id"],
                    "status": row.get("status", "open") if hasattr(row, "get") else row["status"],
                    "summary": row.get("summary", "") if hasattr(row, "get") else row["summary"],
                }
            except Exception:
                return None
        return None

    if not params.get("correlate_upstream", True):
        return None

    asset_object = f"ASSET:{asset_uuid}"
    try:
        row = await conn.fetchrow(
            """
            SELECT st.id, st.asset_id, st.status, st.summary
            FROM kg_edges ke
            JOIN service_tickets st ON (
                ke.subject_label = 'ASSET:' || st.asset_id::text
                OR ke.object_label = 'ASSET:' || st.asset_id::text
            )
            WHERE ke.namespace_id = $1::uuid
              AND st.namespace_id = $1::uuid
              AND (
                  (ke.object_label = $2 AND ke.predicate IN ('connected_to', 'feeds', 'powers'))
                  OR
                  (ke.subject_label = $2 AND ke.predicate IN ('depends_on', 'powered_by'))
              )
              AND st.status IN ('open', 'in_progress', 'waiting_parts')
              AND (st.change_origin = 'proactive_telemetry' OR (st.change_origin = 'agent' AND source = 'nce'))
            ORDER BY st.created_at DESC
            LIMIT 1
            """,
            ns_uuid,
            asset_object,
        )
    except Exception as exc:
        log.debug("Upstream correlation query skipped/failed: %s", exc)
        return None

    if row is not None and hasattr(row, "__getitem__"):
        try:
            return {
                "id": row["id"],
                "asset_id": row["asset_id"],
                "status": row.get("status", "open") if hasattr(row, "get") else row["status"],
                "summary": row.get("summary", "") if hasattr(row, "get") else row["summary"],
            }
        except Exception:
            return None
    return None


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
              AND (change_origin = 'proactive_telemetry' OR (change_origin = 'agent' AND source = 'nce'))
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

        # 1b. Upstream Topological Root-Cause Correlation (I-VERT-01)
        if params.get("correlate_upstream", True):
            upstream_ticket = await _find_upstream_root_cause_ticket(
                conn, ns_uuid, asset_uuid, params
            )
            if upstream_ticket is not None:
                upstream_ticket_id = upstream_ticket["id"]
                upstream_asset_id = upstream_ticket.get("asset_id")
                log.info(
                    "Correlated proactive anomaly for asset_id=%s to upstream root cause ticket_id=%s (asset_id=%s)",
                    asset_uuid,
                    upstream_ticket_id,
                    upstream_asset_id,
                )
                return {
                    "ok": True,
                    "status": "correlated_to_upstream",
                    "ticket_id": str(upstream_ticket_id),
                    "root_cause_asset_id": str(upstream_asset_id) if upstream_asset_id else None,
                    "created": False,
                }

        # 2. Contract A Ownership Guard: Assert Support owns TICKET
        await assert_owner(conn, ns_uuid, _NODE_TYPE_TICKET, _SUPPORT_ENGINE)

    # 3. Create native ServiceTicket
    open_params = {
        "namespace_id": str(ns_uuid),
        "asset_id": str(asset_uuid),
        "summary": summary,
        "priority": priority,
        "change_origin": "agent",
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
                $1, 'about', $2, 1.0, $3::uuid, 'agent'
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


do_proactive_ticket_from_anomaly = do_open_proactive_telemetry_ticket
