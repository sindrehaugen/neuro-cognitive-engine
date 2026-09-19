"""
nce/vertical_modules/support/summary.py
=======================================
Wave D-6: Ticket summary & links for Module 10 (Support Engine):
  - do_summarise_ticket: C9a retrieval-grounded generation synthesizing a
    verified ticket summary strictly from DB facts (kg_nodes) via ground()
  - do_get_ticket_links: retrieves linked entities (FUNCTIONAL_LOCATION,
    AGREEMENT, ASSET, WORK_ORDER) from kg_edges and service_tickets
  - do_link_ticket: associates a ticket with a target entity (FL, agreement,
    asset, external space) by asserting ownership and inserting a kg_edges
    boundary edge, updating ticket attributes, and logging an audit event.

Strict Tenant Predicate Discipline (Charter §5.5)
-------------------------------------------------
Every query against tenant tables (service_tickets, support_ticket_actions,
sla_clocks, kg_nodes, kg_edges) enforces explicit WHERE namespace_id = $N::uuid.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.structural.grounded import ground
from nce.vertical_modules.support._guard import require_support_enabled
from nce.vertical_modules.support.tickets import (
    TicketNotFoundError,
    _extract_pool,
    _parse_uuid,
)

log = logging.getLogger("nce.vertical_modules.support.summary")

_SUPPORT_ENGINE: str = "support"
_NODE_TYPE_TICKET: str = "TICKET"

_ALLOWED_LINK_TARGET_TYPES = frozenset(
    {
        "functional_location",
        "agreement",
        "asset",
        "external_space",
        "work_order",
    }
)


async def do_summarise_ticket(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Generate a C9a-grounded summary of a support ticket.

    Constructs verified factual claims from service_tickets, support_ticket_actions,
    sla_clocks, and kg_edges, upserts them into kg_nodes (TICKET entity type),
    and resolves them through nce.structural.grounded.ground.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id: (required) support ticket UUID string or UUID.

    Returns
    -------
    dict with:
        - ticket_id: str
        - prose: str (grounded narrative)
        - citations: list[{"node_id": str, "fact": str}]
        - dropped: list[{"node_id": str}]
        - ticket: dict of ticket attributes
        - action_count: int
        - link_count: int
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id_raw = params.get("ticket_id")
    if not ticket_id_raw:
        raise ValueError("do_summarise_ticket: ticket_id is required")
    ticket_uuid = _parse_uuid(ticket_id_raw, "ticket_id")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        await require_support_enabled(conn, ns_uuid)

        # 1. Fetch ticket record
        ticket_row = await conn.fetchrow(
            """
            SELECT id, namespace_id, source, source_id, asset_id, room_id,
                   customer_id, status, priority, summary, description,
                   sla_profile, first_response_at, resolved_at, ai_diagnosis,
                   events, change_origin, created_at, updated_at
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_uuid,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_uuid))

        # 2. Fetch recent ticket actions (up to 20)
        action_rows = await conn.fetch(
            """
            SELECT id, action_type, action_summary, outcome, performed_by, performed_at
            FROM support_ticket_actions
            WHERE ticket_id = $1::uuid AND namespace_id = $2::uuid
            ORDER BY performed_at ASC, created_at ASC
            LIMIT 20
            """,
            ticket_uuid,
            ns_uuid,
        )

        # 3. Fetch SLA clock status
        sla_row = await conn.fetchrow(
            """
            SELECT sla_profile, is_breached, running_stage,
                   time_to_first_response_breach_at, time_to_resolution_breach_at
            FROM sla_clocks
            WHERE ticket_id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_uuid,
            ns_uuid,
        )

        # 4. Fetch boundary links from kg_edges
        ticket_label = f"TICKET:{ticket_uuid}"
        edge_rows = await conn.fetch(
            """
            SELECT subject_label, predicate, object_label, confidence
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND (subject_label = $2 OR object_label = $2)
            ORDER BY created_at ASC
            LIMIT 20
            """,
            ns_uuid,
            ticket_label,
        )

        # 5. Build candidate facts to ground
        raw_facts: list[str] = []
        ticket_core_fact = (
            f"Ticket {ticket_uuid}: {ticket_row['summary']}. "
            f"Priority: {ticket_row['priority']}, Status: {ticket_row['status']}, "
            f"SLA Profile: {ticket_row['sla_profile']}."
        )
        raw_facts.append(ticket_core_fact)

        if ticket_row["description"]:
            clean_desc = " ".join(str(ticket_row["description"]).strip().split()[:20])
            raw_facts.append(f"Description: {clean_desc}")

        if ticket_row["customer_id"]:
            raw_facts.append(f"Customer identifier: {ticket_row['customer_id']}.")

        if ticket_row["room_id"]:
            raw_facts.append(f"Functional location room: {ticket_row['room_id']}.")

        if ticket_row["asset_id"]:
            raw_facts.append(f"Target equipment asset: {ticket_row['asset_id']}.")

        if sla_row:
            sla_breached_str = "BREACHED" if sla_row["is_breached"] else "in-compliance"
            raw_facts.append(
                f"SLA Status: {sla_breached_str} (running stage: {sla_row['running_stage']})."
            )

        for act in action_rows:
            raw_facts.append(
                f"Intervention ({act['action_type']}): {act['action_summary']} "
                f"by {act['performed_by']} -> outcome: {act['outcome']}."
            )

        for edge in edge_rows:
            other_label = (
                edge["object_label"]
                if edge["subject_label"] == ticket_label
                else edge["subject_label"]
            )
            raw_facts.append(f"Linked via relation '{edge['predicate']}' to {other_label}.")

        # 6. Assert ownership and upsert facts into kg_nodes
        await assert_owner(conn, ns_uuid, _NODE_TYPE_TICKET, _SUPPORT_ENGINE)

        claims: list[dict[str, str | UUID]] = []
        for idx, fact_text in enumerate(raw_facts):
            node_label = f"TICKET:{ticket_uuid}:FACT:{idx}:{fact_text[:120]}"
            node_id = await conn.fetchval(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, $2, $3::uuid, 'agent')
                ON CONFLICT (label, namespace_id)
                DO UPDATE SET updated_at = now()
                RETURNING id
                """,
                node_label,
                _NODE_TYPE_TICKET,
                ns_uuid,
            )
            claims.append({"node_id": node_id})

        # 7. Execute C9a retrieval-grounded synthesis
        template = "Support Ticket Summary: {facts}"
        ground_result = await ground(
            conn,
            namespace_id=ns_uuid,
            claims=claims,
            template=template,
        )

        ticket_dict = {
            "id": str(ticket_row["id"]),
            "namespace_id": str(ticket_row["namespace_id"]),
            "status": ticket_row["status"],
            "priority": ticket_row["priority"],
            "summary": ticket_row["summary"],
            "description": ticket_row["description"],
            "sla_profile": ticket_row["sla_profile"],
            "room_id": ticket_row["room_id"],
            "asset_id": str(ticket_row["asset_id"]) if ticket_row["asset_id"] else None,
            "customer_id": ticket_row["customer_id"],
            "created_at": ticket_row["created_at"].isoformat()
            if ticket_row["created_at"]
            else None,
            "resolved_at": ticket_row["resolved_at"].isoformat()
            if ticket_row["resolved_at"]
            else None,
        }

        return {
            "ok": True,
            "ticket_id": str(ticket_uuid),
            "prose": ground_result["prose"],
            "citations": ground_result["citations"],
            "dropped": ground_result["dropped"],
            "ticket": ticket_dict,
            "action_count": len(action_rows),
            "link_count": len(edge_rows),
        }


async def do_get_ticket_links(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve all linked entities for a ticket.

    Extracts functional locations, agreements, assets, and work orders
    connected via kg_edges and direct attributes on service_tickets.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id: (required) support ticket UUID string or UUID.

    Returns
    -------
    dict with:
        - ticket_id: str
        - functional_locations: list of linked FL references/ids
        - agreements: list of linked agreement references/ids
        - assets: list of linked asset references/ids
        - work_orders: list of linked work order references/ids
        - edges: list of all raw matching edge dicts
        - count: total link count
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id_raw = params.get("ticket_id")
    if not ticket_id_raw:
        raise ValueError("do_get_ticket_links: ticket_id is required")
    ticket_uuid = _parse_uuid(ticket_id_raw, "ticket_id")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        await require_support_enabled(conn, ns_uuid)

        ticket_row = await conn.fetchrow(
            """
            SELECT id, room_id, asset_id, customer_id
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_uuid,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_uuid))

        ticket_label = f"TICKET:{ticket_uuid}"
        edge_rows = await conn.fetch(
            """
            SELECT subject_label, predicate, object_label, confidence, change_origin, created_at
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND (subject_label = $2 OR object_label = $2)
            ORDER BY created_at ASC
            """,
            ns_uuid,
            ticket_label,
        )

        functional_locations: list[dict[str, Any]] = []
        agreements: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        work_orders: list[dict[str, Any]] = []
        all_edges: list[dict[str, Any]] = []

        # Direct attribute links
        if ticket_row["room_id"]:
            functional_locations.append(
                {
                    "target_id": ticket_row["room_id"],
                    "target_type": "functional_location",
                    "relation": "direct_room",
                    "source": "service_tickets.room_id",
                }
            )

        if ticket_row["asset_id"]:
            assets.append(
                {
                    "target_id": str(ticket_row["asset_id"]),
                    "target_type": "asset",
                    "relation": "direct_asset",
                    "source": "service_tickets.asset_id",
                }
            )

        # Graph edge links
        for edge in edge_rows:
            all_edges.append(
                {
                    "subject_label": edge["subject_label"],
                    "predicate": edge["predicate"],
                    "object_label": edge["object_label"],
                    "confidence": edge["confidence"],
                    "change_origin": edge["change_origin"],
                    "created_at": edge["created_at"].isoformat() if edge["created_at"] else None,
                }
            )

            # Determine the linked entity label
            target = (
                edge["object_label"]
                if edge["subject_label"] == ticket_label
                else edge["subject_label"]
            )
            upper_target = target.upper()

            if (
                upper_target.startswith("FUNCTIONAL_LOCATION:")
                or upper_target.startswith("FL:")
                or upper_target.startswith("ROOM:")
            ):
                fl_id = target.split(":", 1)[1] if ":" in target else target
                functional_locations.append(
                    {
                        "target_id": fl_id,
                        "target_label": target,
                        "target_type": "functional_location",
                        "relation": edge["predicate"],
                        "source": "kg_edges",
                    }
                )
            elif upper_target.startswith("AGREEMENT:") or upper_target.startswith("CONTRACT:"):
                agr_id = target.split(":", 1)[1] if ":" in target else target
                agreements.append(
                    {
                        "target_id": agr_id,
                        "target_label": target,
                        "target_type": "agreement",
                        "relation": edge["predicate"],
                        "source": "kg_edges",
                    }
                )
            elif upper_target.startswith("ASSET:"):
                asset_id = target.split(":", 1)[1] if ":" in target else target
                if not any(a["target_id"] == asset_id for a in assets):
                    assets.append(
                        {
                            "target_id": asset_id,
                            "target_label": target,
                            "target_type": "asset",
                            "relation": edge["predicate"],
                            "source": "kg_edges",
                        }
                    )
            elif upper_target.startswith("WORK_ORDER:") or upper_target.startswith("WO:"):
                wo_id = target.split(":", 1)[1] if ":" in target else target
                work_orders.append(
                    {
                        "target_id": wo_id,
                        "target_label": target,
                        "target_type": "work_order",
                        "relation": edge["predicate"],
                        "source": "kg_edges",
                    }
                )

        total_count = len(functional_locations) + len(agreements) + len(assets) + len(work_orders)

        return {
            "ok": True,
            "ticket_id": str(ticket_uuid),
            "functional_locations": functional_locations,
            "agreements": agreements,
            "assets": assets,
            "work_orders": work_orders,
            "edges": all_edges,
            "count": total_count,
        }


async def do_link_ticket(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Link a support ticket to an entity (FL, agreement, asset, external space).

    Establishes a TICKET -[relation]-> TARGET boundary edge in kg_edges,
    updates service_tickets attributes when applicable, and appends an audit event.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id: (required) support ticket UUID string or UUID.
        - target_type: (required) str in {"functional_location", "agreement", "asset", "external_space", "work_order"}.
        - target_id: (required) str target entity identifier.
        - relation: (optional) str edge predicate (defaults to 'about').
        - change_origin: (optional) str origin enum (defaults to 'agent').

    Returns
    -------
    dict with:
        - ok: True
        - ticket_id: str
        - target_type: str
        - target_id: str
        - relation: str
        - edge: str
        - status: "linked"
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id_raw = params.get("ticket_id")
    if not ticket_id_raw:
        raise ValueError("do_link_ticket: ticket_id is required")
    ticket_uuid = _parse_uuid(ticket_id_raw, "ticket_id")

    target_type = (params.get("target_type") or "").strip().lower()
    if target_type not in _ALLOWED_LINK_TARGET_TYPES:
        raise ValueError(
            f"do_link_ticket: invalid target_type '{target_type}'. "
            f"Must be one of: {sorted(_ALLOWED_LINK_TARGET_TYPES)}"
        )

    target_id = str(params.get("target_id") or "").strip()
    if not target_id:
        raise ValueError("do_link_ticket: target_id must be a non-empty string")

    relation = (params.get("relation") or "about").strip()
    if not relation:
        relation = "about"

    change_origin = (params.get("change_origin") or "agent").strip().lower()
    if change_origin not in {
        "sync",
        "webhook",
        "agent",
        "operator",
        "consolidation",
        "replay",
        "unknown",
    }:
        change_origin = "agent"

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    target_object_label = f"{target_type.upper()}:{target_id}"
    ticket_subject_label = f"TICKET:{ticket_uuid}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        await require_support_enabled(conn, ns_uuid)

        ticket_row = await conn.fetchrow(
            """
            SELECT id, room_id, asset_id
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_uuid,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_uuid))

        # 1. Assert Support engine ownership of TICKET
        await assert_owner(conn, ns_uuid, _NODE_TYPE_TICKET, _SUPPORT_ENGINE)

        # 2. Insert or update kg_edges boundary link
        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, $2, $3, 1.0, $4::uuid, $5
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id)
            DO UPDATE SET updated_at = now()
            """,
            ticket_subject_label,
            relation,
            target_object_label,
            ns_uuid,
            change_origin,
        )

        # 3. If target is functional_location and room_id is NULL, update room_id
        if target_type == "functional_location" and not ticket_row["room_id"]:
            await conn.execute(
                """
                UPDATE service_tickets
                SET room_id = $1, updated_at = $2::timestamptz
                WHERE id = $3::uuid AND namespace_id = $4::uuid
                """,
                target_id,
                now_dt,
                ticket_uuid,
                ns_uuid,
            )

        # 4. If target is asset and asset_id is NULL, update asset_id if valid UUID
        if target_type == "asset" and not ticket_row["asset_id"]:
            try:
                target_asset_uuid = UUID(target_id)
                await conn.execute(
                    """
                    UPDATE service_tickets
                    SET asset_id = $1::uuid, updated_at = $2::timestamptz
                    WHERE id = $3::uuid AND namespace_id = $4::uuid
                    """,
                    target_asset_uuid,
                    now_dt,
                    ticket_uuid,
                    ns_uuid,
                )
            except ValueError:
                pass

        # 5. Append ticket_linked event to events JSONB
        link_event = {
            "type": "ticket_linked",
            "target_type": target_type,
            "target_id": target_id,
            "relation": relation,
            "at": now_dt.isoformat(),
            "origin": change_origin,
        }
        await conn.execute(
            """
            UPDATE service_tickets
            SET events = events || $1::jsonb,
                updated_at = $2::timestamptz
            WHERE id = $3::uuid AND namespace_id = $4::uuid
            """,
            json.dumps([link_event]),
            now_dt,
            ticket_uuid,
            ns_uuid,
        )

    return {
        "ok": True,
        "ticket_id": str(ticket_uuid),
        "target_type": target_type,
        "target_id": target_id,
        "relation": relation,
        "edge": f"{ticket_subject_label} -[{relation}]-> {target_object_label}",
        "status": "linked",
    }
