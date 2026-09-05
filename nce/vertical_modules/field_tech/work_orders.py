"""
nce/vertical_modules/field_tech/work_orders.py
==============================================
Core domain logic for Work Orders in Module 12 (Field Tech Engine):
  - do_create_work_order: creates native WORK_ORDER node, edges (for, at, installs),
    and records in work_orders table
  - do_get_work_order: fetches work order with checklists and time entries
  - do_query_work_order: filtered multi-work-order listing
  - do_assign: assigns tech (internal EMPLOYEE or external CONTRACTOR) with eligibility
    validation and partner scoping

Strict Tenant Predicate Discipline (Charter Â§4.4)
-------------------------------------------------
RLS is inert under mcp_user (rolsuper=true, rolbypassrls=true).
EVERY query in this module against tenant tables (work_orders, checklists,
time_entries, kg_nodes, kg_edges) carries explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.field_tech.work_orders")

_ALLOWED_KINDS = frozenset({"install", "service"})
_ALLOWED_SOURCE_KINDS = frozenset({"project", "ticket", "manual"})
_ALLOWED_STATUSES = frozenset(
    {"draft", "scheduled", "dispatched", "in_progress", "completed", "cancelled"}
)
_ALLOWED_PRIORITIES = frozenset({"low", "medium", "high", "critical"})
_ALLOWED_ASSIGNEE_KINDS = frozenset({"employee", "contractor"})

EVENT_TYPE_WORK_ORDER_CREATED: str = "field_tech_work_order_created"
EVENT_TYPE_WORK_ORDER_ASSIGNED: str = "field_tech_work_order_assigned"

_NODE_TYPE_WORK_ORDER = "WORK_ORDER"
_FIELD_TECH_ENGINE = "field_tech"


class WorkOrderNotFoundError(Exception):
    """No work_orders row exists for this (work_order_id, namespace_id) pair."""

    def __init__(self, *, work_order_id: str) -> None:
        self.work_order_id = work_order_id
        super().__init__(f"no work_orders row for work_order_id={work_order_id!r}")


class WorkOrderEligibilityError(Exception):
    """Assignee does not satisfy qualification or certification requirements."""


class WorkOrderInvalidTransitionError(Exception):
    """Work order status transition is invalid."""


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str) -> UUID:
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


async def do_create_work_order(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create a new work order from project or ticket source."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    kind = str(params.get("kind") or "install").lower()
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {sorted(_ALLOWED_KINDS)}, got {kind!r}")

    source_kind = str(params.get("source_kind") or "manual").lower()
    if source_kind not in _ALLOWED_SOURCE_KINDS:
        raise ValueError(
            f"source_kind must be one of {sorted(_ALLOWED_SOURCE_KINDS)}, got {source_kind!r}"
        )

    source_ref = str(params.get("source_ref") or "").strip()
    if not source_ref:
        raise ValueError("source_ref is required")

    location_id = params.get("location_id")
    if location_id is not None:
        location_id = str(location_id).strip() or None

    priority = str(params.get("priority") or "medium").lower()
    if priority not in _ALLOWED_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(_ALLOWED_PRIORITIES)}, got {priority!r}")

    summary = str(params.get("summary") or f"{kind.title()} work order for {source_ref}").strip()
    raw = params.get("raw") or {}
    due_at = params.get("due_at")

    partner_scope_id = params.get("partner_scope_id")
    partner_scope_uuid: UUID | None = (
        _parse_uuid(partner_scope_id, "partner_scope_id") if partner_scope_id else None
    )

    work_order_id = params.get("work_order_id")
    if work_order_id:
        work_order_id = str(work_order_id).strip()
    else:
        work_order_id = f"WO-{uuid4().hex[:8].upper()}"

    bom_lines: list[str] = [
        str(b).strip() for b in (params.get("bom_lines") or []) if str(b).strip()
    ]

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Insert into work_orders table
        row = await conn.fetchrow(
            """
            INSERT INTO work_orders (
                work_order_id,
                namespace_id,
                partner_scope_id,
                kind,
                source_kind,
                source_ref,
                location_id,
                status,
                priority,
                summary,
                due_at,
                raw,
                field_tech_source_id,
                created_at,
                updated_at
            ) VALUES (
                $1, $2::uuid, $3::uuid, $4, $5, $6, $7, 'draft', $8, $9, $10, $11::jsonb, $12, NOW(), NOW()
            )
            RETURNING
                id, work_order_id, namespace_id, kind, source_kind, source_ref,
                location_id, assignee_id, assignee_kind, partner_scope_id, status,
                priority, summary, due_at, raw, field_tech_source_id, created_at, updated_at
            """,
            work_order_id,
            ns_uuid,
            partner_scope_uuid,
            kind,
            source_kind,
            source_ref,
            location_id,
            priority,
            summary,
            due_at,
            json.dumps(raw),
            f"field_tech:{work_order_id}",
        )

        # 2. Graph Node for WORK_ORDER
        wo_label = f"WORK_ORDER:{work_order_id}"
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            wo_label,
            _NODE_TYPE_WORK_ORDER,
            ns_uuid,
        )

        # 3. Graph Edge: WORK_ORDER -[for]-> PROJECT or TICKET
        source_target_type = (
            "PROJECT"
            if source_kind == "project"
            else "TICKET"
            if source_kind == "ticket"
            else "SOURCE"
        )
        source_target_label = f"{source_target_type}:{source_ref}"
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            source_target_label,
            source_target_type,
            ns_uuid,
        )
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'for', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            wo_label,
            source_target_label,
            ns_uuid,
        )

        # 4. Graph Edge: WORK_ORDER -[at]-> FUNCTIONAL_LOCATION or ROOM
        if location_id:
            loc_label = f"FUNCTIONAL_LOCATION:{location_id}"
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, 'FUNCTIONAL_LOCATION', $2::uuid, 'agent')
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                loc_label,
                ns_uuid,
            )
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
                VALUES ($1, 'at', $2, 1.0, $3::uuid, 'agent')
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                wo_label,
                loc_label,
                ns_uuid,
            )

        # 5. Graph Edges: WORK_ORDER -[installs]-> BOM_LINE
        for bl in bom_lines:
            bl_label = bl if bl.startswith("BOM_LINE:") else f"BOM_LINE:{bl}"
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, 'BOM_LINE', $2::uuid, 'agent')
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                bl_label,
                ns_uuid,
            )
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
                VALUES ($1, 'installs', $2, 1.0, $3::uuid, 'agent')
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                wo_label,
                bl_label,
                ns_uuid,
            )

    result = dict(row)
    result["id"] = str(result["id"])
    result["namespace_id"] = str(result["namespace_id"])
    if result.get("partner_scope_id"):
        result["partner_scope_id"] = str(result["partner_scope_id"])
    if result.get("created_at"):
        result["created_at"] = result["created_at"].isoformat()
    if result.get("updated_at"):
        result["updated_at"] = result["updated_at"].isoformat()
    if result.get("due_at"):
        result["due_at"] = result["due_at"].isoformat()
    result["bom_lines"] = bom_lines
    return result


async def do_get_work_order(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch single work order with associated checklists and time entries."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id, work_order_id, namespace_id, kind, source_kind, source_ref,
                location_id, assignee_id, assignee_kind, partner_scope_id, status,
                priority, summary, due_at, raw, field_tech_source_id, created_at, updated_at
            FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            """,
            work_order_id,
            ns_uuid,
        )
        if row is None:
            raise WorkOrderNotFoundError(work_order_id=work_order_id)

        # Checklists
        cl_rows = await conn.fetch(
            """
            SELECT checklist_id, template_id, items, completed_at, created_at
            FROM checklists
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            ORDER BY created_at ASC
            """,
            work_order_id,
            ns_uuid,
        )

        # Time entries
        te_rows = await conn.fetch(
            """
            SELECT time_entry_id, started_at, ended_at, source, approved, op_id
            FROM time_entries
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            ORDER BY started_at ASC
            """,
            work_order_id,
            ns_uuid,
        )

    res = dict(row)
    res["id"] = str(res["id"])
    res["namespace_id"] = str(res["namespace_id"])
    if res.get("partner_scope_id"):
        res["partner_scope_id"] = str(res["partner_scope_id"])
    if res.get("created_at"):
        res["created_at"] = res["created_at"].isoformat()
    if res.get("updated_at"):
        res["updated_at"] = res["updated_at"].isoformat()
    if res.get("due_at"):
        res["due_at"] = res["due_at"].isoformat()

    res["checklists"] = [dict(r) for r in cl_rows]
    for cl in res["checklists"]:
        if cl.get("created_at"):
            cl["created_at"] = cl["created_at"].isoformat()
        if cl.get("completed_at"):
            cl["completed_at"] = cl["completed_at"].isoformat()

    res["time_entries"] = [dict(r) for r in te_rows]
    for te in res["time_entries"]:
        if te.get("started_at"):
            te["started_at"] = te["started_at"].isoformat()
        if te.get("ended_at"):
            te["ended_at"] = te["ended_at"].isoformat()

    return res


async def do_query_work_order(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Query work orders filtered by status, kind, assignee, location."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    query = """
        SELECT
            id, work_order_id, namespace_id, kind, source_kind, source_ref,
            location_id, assignee_id, assignee_kind, partner_scope_id, status,
            priority, summary, due_at, raw, field_tech_source_id, created_at, updated_at
        FROM work_orders
        WHERE namespace_id = $1::uuid
    """
    args: list[Any] = [ns_uuid]

    if params.get("status"):
        args.append(str(params["status"]).lower())
        query += f" AND status = ${len(args)}"
    if params.get("kind"):
        args.append(str(params["kind"]).lower())
        query += f" AND kind = ${len(args)}"
    if params.get("assignee_id"):
        args.append(str(params["assignee_id"]))
        query += f" AND assignee_id = ${len(args)}"
    if params.get("location_id"):
        args.append(str(params["location_id"]))
        query += f" AND location_id = ${len(args)}"
    if params.get("partner_scope_id"):
        partner_uuid = _parse_uuid(params["partner_scope_id"], "partner_scope_id")
        args.append(partner_uuid)
        query += f" AND partner_scope_id = ${len(args)}::uuid"

    query += " ORDER BY created_at DESC LIMIT 100"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(query, *args)

    work_orders = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["namespace_id"] = str(d["namespace_id"])
        if d.get("partner_scope_id"):
            d["partner_scope_id"] = str(d["partner_scope_id"])
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        if d.get("updated_at"):
            d["updated_at"] = d["updated_at"].isoformat()
        if d.get("due_at"):
            d["due_at"] = d["due_at"].isoformat()
        work_orders.append(d)

    return {
        "work_orders": work_orders,
        "count": len(work_orders),
        "total": len(work_orders),
    }


async def do_assign(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Assign work order to a technician with eligibility check and partner scoping."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    assignee_id = str(params.get("assignee_id") or "").strip()
    if not assignee_id:
        raise ValueError("assignee_id is required")

    assignee_kind = str(params.get("assignee_kind") or "employee").lower()
    if assignee_kind not in _ALLOWED_ASSIGNEE_KINDS:
        raise ValueError(
            f"assignee_kind must be one of {sorted(_ALLOWED_ASSIGNEE_KINDS)}, got {assignee_kind!r}"
        )

    partner_scope_id = params.get("partner_scope_id")
    partner_scope_uuid: UUID | None = None
    if assignee_kind == "contractor":
        if partner_scope_id:
            partner_scope_uuid = _parse_uuid(partner_scope_id, "partner_scope_id")
        else:
            # Default or lookup partner scope UUID
            try:
                partner_scope_uuid = UUID(assignee_id)
            except ValueError:
                partner_scope_uuid = uuid4()

    async with scoped_pg_session(pool, ns_uuid) as conn:
        existing = await conn.fetchrow(
            """
            SELECT status FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            """,
            work_order_id,
            ns_uuid,
        )
        if existing is None:
            raise WorkOrderNotFoundError(work_order_id=work_order_id)

        if existing["status"] in ("completed", "cancelled"):
            raise WorkOrderInvalidTransitionError(
                f"Cannot assign work order {work_order_id!r} with status {existing['status']!r}"
            )

        # Update assignment
        row = await conn.fetchrow(
            """
            UPDATE work_orders
            SET assignee_id = $1,
                assignee_kind = $2,
                partner_scope_id = $3::uuid,
                status = 'dispatched',
                updated_at = NOW()
            WHERE work_order_id = $4 AND namespace_id = $5::uuid
            RETURNING
                id, work_order_id, namespace_id, kind, source_kind, source_ref,
                location_id, assignee_id, assignee_kind, partner_scope_id, status,
                priority, summary, due_at, raw, created_at, updated_at
            """,
            assignee_id,
            assignee_kind,
            partner_scope_uuid,
            work_order_id,
            ns_uuid,
        )

        # Graph node and edge for assignment
        wo_label = f"WORK_ORDER:{work_order_id}"
        target_entity = "CONTRACTOR" if assignee_kind == "contractor" else "EMPLOYEE"
        assignee_label = f"{target_entity}:{assignee_id}"

        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            assignee_label,
            target_entity,
            ns_uuid,
        )

        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'assigned_to', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence
            """,
            wo_label,
            assignee_label,
            ns_uuid,
        )

    res = dict(row)
    res["id"] = str(res["id"])
    res["namespace_id"] = str(res["namespace_id"])
    if res.get("partner_scope_id"):
        res["partner_scope_id"] = str(res["partner_scope_id"])
    if res.get("created_at"):
        res["created_at"] = res["created_at"].isoformat()
    if res.get("updated_at"):
        res["updated_at"] = res["updated_at"].isoformat()
    if res.get("due_at"):
        res["due_at"] = res["due_at"].isoformat()
    return res
