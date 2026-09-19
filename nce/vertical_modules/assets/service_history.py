"""
nce/vertical_modules/assets/service_history.py
================================================
Wave D-3: Asset Service History Read Core.

Composes service history for an ASSET by aggregating:
  1. Support Tickets (``service_tickets`` direct asset_id FK or boundary
     ``TICKET -[about]-> ASSET`` edges in ``kg_edges``).
  2. Ticket Actions & Outcomes (``support_ticket_actions``: interventions and outcomes).
  3. Field Tech Work Orders (``work_orders`` originating from tickets, targeting
     the asset, or linked via ``kg_edges``).
  4. Outcome & Boundary Edges (``kg_edges`` including ``failure_pattern``,
     ``dispatched_as``, ``for``, and outcome relations).
  5. Chronological unified service timeline.

Strictly multi-tenant: every database query carries explicit
``WHERE namespace_id = $1::uuid`` predicates.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.assets.service_history")


def _extract_pool(engine: Any) -> Any:
    return getattr(engine, "pg_pool", engine)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


async def do_get_asset_service_history(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve full composite service history for a specific asset.

    Parameters
    ----------
    engine:
        NCEEngine instance or asyncpg.Pool.
    params:
        ``namespace_id``: str | UUID (required)
        ``asset_id``: str | UUID (required)
        ``limit``: int, optional (default 50, max 200)
        ``order``: str, optional ('desc' default, or 'asc')

    Returns
    -------
    dict with:
        ``ok``: bool
        ``asset_id``: str
        ``asset``: dict summary
        ``timeline``: list of chronological event records
        ``tickets``: list of support ticket records
        ``work_orders``: list of work order records
        ``actions``: list of ticket action records
        ``outcome_edges``: list of boundary/outcome graph edges
        ``summary``: aggregate counts
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("Missing required parameter: namespace_id")
    try:
        ns_uuid = UUID(str(raw_ns))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid namespace_id UUID: {raw_ns!r}") from exc

    raw_asset = params.get("asset_id")
    if not raw_asset:
        raise ValueError("Missing required parameter: asset_id")
    try:
        asset_uuid = UUID(str(raw_asset))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid asset_id UUID: {raw_asset!r}") from exc

    limit = min(max(int(params.get("limit") or 50), 1), 200)
    order = str(params.get("order") or "desc").strip().lower()
    if order not in ("asc", "desc"):
        order = "desc"

    pool = _extract_pool(engine)
    asset_label = f"ASSET:{asset_uuid}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Verify asset existence and retrieve base record
        asset_row = await conn.fetchrow(
            """
            SELECT id, namespace_id, serial, lifecycle_state, is_shell,
                   product_id, product_sku, functional_location_id, bom_line_id,
                   created_at, updated_at
            FROM assets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            asset_uuid,
            ns_uuid,
        )
        if asset_row is None:
            return {
                "ok": False,
                "not_found": True,
                "asset_id": str(asset_uuid),
                "error": f"Asset '{asset_uuid}' not found",
            }

        asset_dict = {
            "id": str(asset_row["id"]),
            "serial": asset_row["serial"],
            "lifecycle_state": asset_row["lifecycle_state"],
            "is_shell": bool(asset_row["is_shell"]),
            "product_id": str(asset_row["product_id"]) if asset_row["product_id"] else None,
            "product_sku": asset_row["product_sku"],
            "functional_location_id": asset_row["functional_location_id"],
            "bom_line_id": asset_row["bom_line_id"],
            "created_at": _iso(asset_row["created_at"]),
            "updated_at": _iso(asset_row["updated_at"]),
        }

        # 2. Find boundary graph edges referencing ASSET in kg_edges
        asset_edges = await conn.fetch(
            """
            SELECT id, subject_label, predicate, object_label, confidence,
                   change_origin, created_at, updated_at
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND (subject_label = $2 OR object_label = $2)
            """,
            ns_uuid,
            asset_label,
        )

        linked_ticket_uuids: set[UUID] = set()
        linked_wo_ids: set[str] = set()
        boundary_edges: list[dict[str, Any]] = []

        for edge in asset_edges:
            boundary_edges.append(
                {
                    "id": str(edge["id"]),
                    "subject_label": edge["subject_label"],
                    "predicate": edge["predicate"],
                    "object_label": edge["object_label"],
                    "confidence": float(edge["confidence"])
                    if edge["confidence"] is not None
                    else 1.0,
                    "created_at": _iso(edge["created_at"]),
                    "updated_at": _iso(edge["updated_at"]),
                }
            )
            # Check for linked ticket in subject/object
            for label in (edge["subject_label"], edge["object_label"]):
                if label.startswith("TICKET:"):
                    try:
                        linked_ticket_uuids.add(UUID(label.split(":", 1)[1]))
                    except (ValueError, IndexError):
                        pass
                elif label.startswith("WORK_ORDER:"):
                    try:
                        linked_wo_ids.add(label.split(":", 1)[1])
                    except IndexError:
                        pass

        # 3. Retrieve tickets (direct asset_id FK or boundary graph edge)
        ticket_filter_uuids = list(linked_ticket_uuids)
        ticket_rows = await conn.fetch(
            """
            SELECT id, source, source_id, asset_id, room_id, customer_id,
                   status, priority, summary, description, sla_profile,
                   first_response_at, resolved_at, ai_diagnosis, events,
                   created_at, updated_at
            FROM service_tickets
            WHERE namespace_id = $1::uuid
              AND (asset_id = $2::uuid OR id = ANY($3::uuid[]))
            ORDER BY created_at ASC
            """,
            ns_uuid,
            asset_uuid,
            ticket_filter_uuids,
        )

        tickets: list[dict[str, Any]] = []
        ticket_uuids: list[UUID] = []
        ticket_ids_str: list[str] = []

        for tr in ticket_rows:
            t_uuid = tr["id"]
            ticket_uuids.append(t_uuid)
            ticket_ids_str.append(str(t_uuid))
            tickets.append(
                {
                    "id": str(t_uuid),
                    "source": tr["source"],
                    "source_id": tr["source_id"],
                    "asset_id": str(tr["asset_id"]) if tr["asset_id"] else None,
                    "room_id": tr["room_id"],
                    "customer_id": tr["customer_id"],
                    "status": tr["status"],
                    "priority": tr["priority"],
                    "summary": tr["summary"],
                    "description": tr["description"],
                    "sla_profile": tr["sla_profile"],
                    "first_response_at": _iso(tr["first_response_at"]),
                    "resolved_at": _iso(tr["resolved_at"]),
                    "ai_diagnosis": tr["ai_diagnosis"] or {},
                    "events": tr["events"] or [],
                    "created_at": _iso(tr["created_at"]),
                    "updated_at": _iso(tr["updated_at"]),
                }
            )

        # 4. Retrieve ticket actions for all identified tickets
        actions: list[dict[str, Any]] = []
        if ticket_uuids:
            action_rows = await conn.fetch(
                """
                SELECT id, namespace_id, ticket_id, action_type, action_summary,
                       action_details, outcome, outcome_notes, performed_by,
                       performed_at, created_at, updated_at
                FROM support_ticket_actions
                WHERE namespace_id = $1::uuid
                  AND ticket_id = ANY($2::uuid[])
                ORDER BY performed_at ASC
                """,
                ns_uuid,
                ticket_uuids,
            )
            for ar in action_rows:
                actions.append(
                    {
                        "id": str(ar["id"]),
                        "ticket_id": str(ar["ticket_id"]),
                        "action_type": ar["action_type"],
                        "action_summary": ar["action_summary"],
                        "action_details": ar["action_details"],
                        "outcome": ar["outcome"],
                        "outcome_notes": ar["outcome_notes"],
                        "performed_by": ar["performed_by"],
                        "performed_at": _iso(ar["performed_at"]),
                        "created_at": _iso(ar["created_at"]),
                        "updated_at": _iso(ar["updated_at"]),
                    }
                )

        # 5. Retrieve graph edges linking tickets to work orders
        if ticket_ids_str:
            ticket_labels = [f"TICKET:{tid}" for tid in ticket_ids_str]
            ticket_edges = await conn.fetch(
                """
                SELECT id, subject_label, predicate, object_label, confidence,
                       change_origin, created_at, updated_at
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND (subject_label = ANY($2::text[]) OR object_label = ANY($2::text[]))
                """,
                ns_uuid,
                ticket_labels,
            )
            for edge in ticket_edges:
                edge_dict = {
                    "id": str(edge["id"]),
                    "subject_label": edge["subject_label"],
                    "predicate": edge["predicate"],
                    "object_label": edge["object_label"],
                    "confidence": float(edge["confidence"])
                    if edge["confidence"] is not None
                    else 1.0,
                    "created_at": _iso(edge["created_at"]),
                    "updated_at": _iso(edge["updated_at"]),
                }
                if edge_dict not in boundary_edges:
                    boundary_edges.append(edge_dict)

                for label in (edge["subject_label"], edge["object_label"]):
                    if label.startswith("WORK_ORDER:"):
                        try:
                            linked_wo_ids.add(label.split(":", 1)[1])
                        except IndexError:
                            pass

        # 6. Retrieve Work Orders (from tickets, asset references, or graph links)
        wo_id_filter = list(linked_wo_ids)
        wo_rows = await conn.fetch(
            """
            SELECT id, work_order_id, namespace_id, partner_scope_id, kind,
                   source_kind, source_ref, location_id, assignee_id, assignee_kind,
                   status, priority, summary, due_at, raw, created_at, updated_at
            FROM work_orders
            WHERE namespace_id = $1::uuid
              AND (
                (source_kind = 'ticket' AND source_ref = ANY($2::text[]))
                OR work_order_id = ANY($3::text[])
                OR raw->>'asset_id' = $4
              )
            ORDER BY created_at ASC
            """,
            ns_uuid,
            ticket_ids_str,
            wo_id_filter,
            str(asset_uuid),
        )

        work_orders: list[dict[str, Any]] = []
        for wor in wo_rows:
            work_orders.append(
                {
                    "id": str(wor["id"]),
                    "work_order_id": wor["work_order_id"],
                    "kind": wor["kind"],
                    "source_kind": wor["source_kind"],
                    "source_ref": wor["source_ref"],
                    "location_id": wor["location_id"],
                    "assignee_id": wor["assignee_id"],
                    "assignee_kind": wor["assignee_kind"],
                    "status": wor["status"],
                    "priority": wor["priority"],
                    "summary": wor["summary"],
                    "due_at": _iso(wor["due_at"]),
                    "raw": wor["raw"] or {},
                    "created_at": _iso(wor["created_at"]),
                    "updated_at": _iso(wor["updated_at"]),
                }
            )

        # 7. Collect outcome edges (edges with outcome/failure_pattern predicates)
        outcome_edges: list[dict[str, Any]] = []
        for edge in boundary_edges:
            pred = edge["predicate"].lower()
            if any(
                term in pred
                for term in ("outcome", "failure", "resolv", "result", "dispatched", "about", "for")
            ):
                outcome_edges.append(edge)

        # 8. Build Unified Chronological Service Timeline
        timeline: list[dict[str, Any]] = []

        # 8a. Ticket creation events
        for t in tickets:
            timeline.append(
                {
                    "event_type": "ticket_opened",
                    "timestamp": t["created_at"],
                    "ref_type": "ticket",
                    "ref_id": t["id"],
                    "summary": f"Support ticket #{t['id'][:8]} opened ({t['priority']}): {t['summary']}",
                    "status": t["status"],
                    "details": {
                        "ticket_id": t["id"],
                        "priority": t["priority"],
                        "status": t["status"],
                        "summary": t["summary"],
                        "sla_profile": t["sla_profile"],
                    },
                }
            )
            # Resolution event
            if t["resolved_at"]:
                timeline.append(
                    {
                        "event_type": "ticket_resolved",
                        "timestamp": t["resolved_at"],
                        "ref_type": "ticket",
                        "ref_id": t["id"],
                        "summary": f"Support ticket #{t['id'][:8]} marked resolved",
                        "status": t["status"],
                        "details": {
                            "ticket_id": t["id"],
                            "resolved_at": t["resolved_at"],
                            "summary": t["summary"],
                        },
                    }
                )

        # 8b. Action & Outcome events
        for act in actions:
            timeline.append(
                {
                    "event_type": "ticket_action",
                    "timestamp": act["performed_at"] or act["created_at"],
                    "ref_type": "ticket_action",
                    "ref_id": act["id"],
                    "summary": f"Action [{act['action_type']}]: {act['action_summary']}",
                    "outcome": act["outcome"],
                    "status": act["outcome"],
                    "details": {
                        "action_id": act["id"],
                        "ticket_id": act["ticket_id"],
                        "action_type": act["action_type"],
                        "action_summary": act["action_summary"],
                        "action_details": act["action_details"],
                        "outcome": act["outcome"],
                        "outcome_notes": act["outcome_notes"],
                        "performed_by": act["performed_by"],
                    },
                }
            )

        # 8c. Work Order events
        for wo in work_orders:
            wo_outcome_meta = (
                wo.get("raw", {}).get("outcome") if isinstance(wo.get("raw"), dict) else None
            )
            timeline.append(
                {
                    "event_type": "work_order",
                    "timestamp": wo["created_at"],
                    "ref_type": "work_order",
                    "ref_id": wo["work_order_id"],
                    "summary": f"Work order {wo['work_order_id']} [{wo['kind']}]: {wo['summary'] or 'Field service'}",
                    "status": wo["status"],
                    "outcome": (
                        wo_outcome_meta.get("rating")
                        if isinstance(wo_outcome_meta, dict)
                        else wo["status"]
                    ),
                    "details": {
                        "work_order_id": wo["work_order_id"],
                        "kind": wo["kind"],
                        "status": wo["status"],
                        "priority": wo["priority"],
                        "assignee_id": wo["assignee_id"],
                        "assignee_kind": wo["assignee_kind"],
                        "outcome_meta": wo_outcome_meta,
                    },
                }
            )

        # 8d. Graph failure pattern edges
        for edge in outcome_edges:
            if edge["predicate"] == "failure_pattern":
                timeline.append(
                    {
                        "event_type": "failure_pattern_recorded",
                        "timestamp": edge["created_at"],
                        "ref_type": "graph_edge",
                        "ref_id": edge["id"],
                        "summary": f"Observed failure pattern linked to {edge['object_label']}",
                        "status": "recorded",
                        "details": {
                            "edge_id": edge["id"],
                            "subject": edge["subject_label"],
                            "predicate": edge["predicate"],
                            "object": edge["object_label"],
                            "confidence": edge["confidence"],
                        },
                    }
                )

        # Sort timeline
        timeline.sort(
            key=lambda x: x["timestamp"] or "",
            reverse=(order == "desc"),
        )
        if len(timeline) > limit:
            timeline = timeline[:limit]

        # 9. Build summary metrics
        outcome_breakdown: dict[str, int] = {}
        for act in actions:
            outc = act["outcome"]
            outcome_breakdown[outc] = outcome_breakdown.get(outc, 0) + 1

        summary = {
            "total_tickets": len(tickets),
            "open_tickets": sum(
                1 for t in tickets if t["status"] not in ("resolved", "closed", "cancelled")
            ),
            "resolved_tickets": sum(1 for t in tickets if t["status"] in ("resolved", "closed")),
            "total_actions": len(actions),
            "total_work_orders": len(work_orders),
            "completed_work_orders": sum(1 for wo in work_orders if wo["status"] == "completed"),
            "action_outcomes": outcome_breakdown,
            "boundary_edges_count": len(boundary_edges),
        }

    return {
        "ok": True,
        "asset_id": str(asset_uuid),
        "asset": asset_dict,
        "summary": summary,
        "timeline": timeline,
        "tickets": tickets,
        "work_orders": work_orders,
        "actions": actions,
        "outcome_edges": outcome_edges,
    }
