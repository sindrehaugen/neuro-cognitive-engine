"""
nce/vertical_modules/field_tech/checklist.py
============================================
Checklist domain logic for Module 12 (Field Tech Engine):
  - do_complete_checklist: records checklist items as ISO9001 quality verification record,
    validates required item completion, creates FIELD_TECH_CHECKLIST graph node and edge.

Strict Tenant Predicate Discipline (Charter Â§4.4)
-------------------------------------------------
EVERY query against checklists / work_orders carries explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.field_tech.checklist")

EVENT_TYPE_CHECKLIST_COMPLETED: str = "field_tech_checklist_completed"
_NODE_TYPE_CHECKLIST = "FIELD_TECH_CHECKLIST"

_TEMPLATES_PATH = (
    Path(__file__).resolve().parents[2] / "config_data" / "field-tech-checklist-templates.json"
)


class ChecklistNotFoundError(Exception):
    """No checklist row found."""


class ChecklistIncompleteError(Exception):
    """Checklist has required items that have not been completed/verified."""


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


def _load_template_items(template_id: str) -> list[dict[str, Any]]:
    if not _TEMPLATES_PATH.exists():
        return []
    try:
        data = json.loads(_TEMPLATES_PATH.read_text(encoding="utf-8"))
        tmpl = data.get(template_id)
        if tmpl and isinstance(tmpl, dict) and "items" in tmpl:
            return list(tmpl["items"])
    except Exception as exc:
        log.warning("Failed to read checklist template %s: %s", template_id, exc)
    return []


async def do_complete_checklist(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record completed items for a work order checklist and verify ISO9001 compliance."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    checklist_id = params.get("checklist_id")
    if checklist_id:
        checklist_id = str(checklist_id).strip()
    else:
        checklist_id = f"CL-{uuid4().hex[:8].upper()}"

    template_id = str(params.get("template_id") or "install_standard").strip()
    items: list[dict[str, Any]] = list(params.get("items") or [])

    # If items are empty or sparse, merge with template items
    template_items = _load_template_items(template_id)
    if template_items and not items:
        items = [dict(it, ticked=False) for it in template_items]
    elif template_items:
        # Overlay required flag from template if missing
        template_req = {it["id"]: it.get("required", False) for it in template_items}
        for it in items:
            if "required" not in it and it.get("id") in template_req:
                it["required"] = template_req[it["id"]]

    # Audit required items
    missing_required = [
        it.get("id") or it.get("label")
        for it in items
        if it.get("required") and not it.get("ticked")
    ]
    strict_quality_gate = params.get("require_all_required", False)
    if strict_quality_gate and missing_required:
        raise ChecklistIncompleteError(
            f"Cannot complete checklist {checklist_id!r}: missing required items: {missing_required}"
        )

    completed = len(missing_required) == 0

    partner_scope_id = params.get("partner_scope_id")
    partner_scope_uuid: UUID | None = None
    if partner_scope_id:
        partner_scope_uuid = _parse_uuid(partner_scope_id, "partner_scope_id")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # Assert work order exists in this namespace
        wo = await conn.fetchrow(
            """
            SELECT partner_scope_id FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            """,
            work_order_id,
            ns_uuid,
        )
        if wo is None:
            raise ValueError(f"Work order {work_order_id!r} not found in namespace")

        if partner_scope_uuid is None and wo["partner_scope_id"] is not None:
            partner_scope_uuid = wo["partner_scope_id"]

        row = await conn.fetchrow(
            """
            INSERT INTO checklists (
                checklist_id,
                work_order_id,
                namespace_id,
                partner_scope_id,
                template_id,
                items,
                completed_at,
                raw,
                created_at,
                updated_at
            ) VALUES (
                $1, $2, $3::uuid, $4::uuid, $5, $6::jsonb,
                CASE WHEN $7::boolean THEN NOW() ELSE NULL END,
                $8::jsonb, NOW(), NOW()
            )
            ON CONFLICT (checklist_id, namespace_id) DO UPDATE SET
                items = EXCLUDED.items,
                completed_at = EXCLUDED.completed_at,
                updated_at = NOW()
            RETURNING
                id, checklist_id, work_order_id, namespace_id, partner_scope_id,
                template_id, items, completed_at, raw, created_at, updated_at
            """,
            checklist_id,
            work_order_id,
            ns_uuid,
            partner_scope_uuid,
            template_id,
            json.dumps(items),
            completed,
            json.dumps(params.get("raw") or {}),
        )

        # Graph node & edge
        cl_label = f"{_NODE_TYPE_CHECKLIST}:{checklist_id}"
        wo_label = f"WORK_ORDER:{work_order_id}"

        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            cl_label,
            _NODE_TYPE_CHECKLIST,
            ns_uuid,
        )

        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'has_checklist', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            wo_label,
            cl_label,
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
    if res.get("completed_at"):
        res["completed_at"] = res["completed_at"].isoformat()
    res["missing_required"] = missing_required
    res["is_complete"] = completed
    return res
