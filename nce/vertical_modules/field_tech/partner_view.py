"""
nce/vertical_modules/field_tech/partner_view.py
===============================================
Partner Access Model enforcement for Module 12 (Field Tech Engine):
  - do_partner_view: field-redacted, partner-scoped work order projection for external
    contractors, strictly filtering to the contractor's own assigned work orders and
    stripping all margin, price, strategy, customer-pipeline and financial data (allow-list only).

Strict Tenant & Partner Scope Discipline (Charter Â§5 & Â§4.4)
------------------------------------------------------------
EVERY query against tenant tables carries explicit WHERE namespace_id = $1::uuid
AND partner_scope_id = $2::uuid.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.field_tech.partner_view")

# Allow-list projection per Partner Access Model layer 3 (Spec Â§50)
_PARTNER_ALLOWED_WO_FIELDS = frozenset(
    {
        "work_order_id",
        "kind",
        "location_id",
        "status",
        "priority",
        "summary",
        "due_at",
        "created_at",
        "updated_at",
    }
)


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


async def do_partner_view(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve partner-scoped and field-redacted work order projection for external contractors."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    partner_uuid = _parse_uuid(params.get("partner_scope_id"), "partner_scope_id")

    work_order_id = params.get("work_order_id")
    if work_order_id:
        work_order_id = str(work_order_id).strip()

    query = """
        SELECT
            work_order_id, kind, location_id, status, priority,
            summary, due_at, created_at, updated_at
        FROM work_orders
        WHERE namespace_id = $1::uuid AND partner_scope_id = $2::uuid
    """
    args: list[Any] = [ns_uuid, partner_uuid]

    if work_order_id:
        args.append(work_order_id)
        query += f" AND work_order_id = ${len(args)}"

    query += " ORDER BY created_at DESC LIMIT 50"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(query, *args)

        results = []
        for r in rows:
            wo_data = {k: r[k] for k in _PARTNER_ALLOWED_WO_FIELDS if k in r}
            wo_id = wo_data["work_order_id"]

            if wo_data.get("created_at"):
                wo_data["created_at"] = wo_data["created_at"].isoformat()
            if wo_data.get("updated_at"):
                wo_data["updated_at"] = wo_data["updated_at"].isoformat()
            if wo_data.get("due_at"):
                wo_data["due_at"] = wo_data["due_at"].isoformat()

            # Checklists (partner scoped)
            cl_rows = await conn.fetch(
                """
                SELECT checklist_id, template_id, items, completed_at
                FROM checklists
                WHERE work_order_id = $1 AND namespace_id = $2::uuid AND partner_scope_id = $3::uuid
                """,
                wo_id,
                ns_uuid,
                partner_uuid,
            )
            wo_data["checklists"] = [
                {
                    "checklist_id": c["checklist_id"],
                    "template_id": c["template_id"],
                    "items": c["items"],
                    "completed_at": c["completed_at"].isoformat() if c["completed_at"] else None,
                }
                for c in cl_rows
            ]

            # Assigned BOM lines (pure technical reference, strictly no pricing/margin)
            bom_edges = await conn.fetch(
                """
                SELECT object_label FROM kg_edges
                WHERE subject_label = $1 AND predicate = 'installs' AND namespace_id = $2::uuid
                """,
                f"WORK_ORDER:{wo_id}",
                ns_uuid,
            )
            wo_data["assigned_bom_lines"] = [
                b["object_label"].replace("BOM_LINE:", "") for b in bom_edges
            ]

            results.append(wo_data)

    ret = {
        "status": "success",
        "partner_scope_id": str(partner_uuid),
        "work_orders": results,
        "count": len(results),
        "redaction": "allow_list_enforced",
    }
    if work_order_id:
        ret["work_order"] = results[0] if results else None
    return ret
