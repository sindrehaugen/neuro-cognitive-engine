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

from nce.bom_lines import update_bom_line_status
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.mcp_errors import BusinessRefusalError

log = logging.getLogger("nce.vertical_modules.field_tech.checklist")

EVENT_TYPE_CHECKLIST_COMPLETED: str = "field_tech_checklist_completed"
_NODE_TYPE_CHECKLIST = "FIELD_TECH_CHECKLIST"
_OWNER_ENGINE = "field_tech"

_TEMPLATES_PATH = (
    Path(__file__).resolve().parents[2] / "config_data" / "field-tech-checklist-templates.json"
)


class ChecklistNotFoundError(BusinessRefusalError):
    """No checklist row found."""


class ChecklistIncompleteError(BusinessRefusalError):
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


def _load_template_kind(template_id: str) -> str:
    if not _TEMPLATES_PATH.exists():
        return "install"
    try:
        data = json.loads(_TEMPLATES_PATH.read_text(encoding="utf-8"))
        tmpl = data.get(template_id)
        if tmpl and isinstance(tmpl, dict) and "kind" in tmpl:
            return str(tmpl["kind"])
    except Exception as exc:
        log.warning("Failed to read checklist template kind %s: %s", template_id, exc)
    return "install"


async def do_submit_checklist(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Submit completed items and quantitative measurements for a work order checklist and verify compliance."""
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
    kind = str(params.get("kind") or _load_template_kind(template_id) or "install")
    items: list[dict[str, Any]] = list(params.get("items") or [])

    # If items are empty or sparse, merge with template items
    template_items = _load_template_items(template_id)
    if template_items and not items:
        items = [dict(it, ticked=False) for it in template_items]
    elif template_items:
        # Overlay required flag and metadata from template if missing
        template_map = {it["id"]: it for it in template_items if "id" in it}
        for it in items:
            tid = it.get("id")
            if tid in template_map:
                t_def = template_map[tid]
                if "required" not in it and "required" in t_def:
                    it["required"] = t_def["required"]
                if "min_value" not in it and "min_value" in t_def:
                    it["min_value"] = t_def["min_value"]
                if "max_value" not in it and "max_value" in t_def:
                    it["max_value"] = t_def["max_value"]
                if "unit" not in it and "unit" in t_def:
                    it["unit"] = t_def["unit"]
                if "label" not in it and "label" in t_def:
                    it["label"] = t_def["label"]

    # Audit required items (boolean completion logic)
    missing_required = [
        it.get("id") or it.get("label")
        for it in items
        if it.get("required") and not (it.get("ticked") or it.get("completed"))
    ]
    completed_count = sum(1 for it in items if (it.get("ticked") or it.get("completed")))
    total_count = len(items)

    # Quantitative threshold validation
    measurement_violations: list[dict[str, Any]] = []
    template_map = {it["id"]: it for it in template_items if "id" in it}
    for it in items:
        tid = it.get("id")
        t_def = template_map.get(tid, {})

        min_val = it.get("min_value")
        if min_val is None:
            min_val = t_def.get("min_value")

        max_val = it.get("max_value")
        if max_val is None:
            max_val = t_def.get("max_value")

        unit = it.get("unit") or t_def.get("unit")

        val: float | None = None
        if "measured_value" in it and it["measured_value"] is not None:
            try:
                val = float(it["measured_value"])
            except (ValueError, TypeError):
                val = None
        elif "value" in it and it["value"] is not None:
            try:
                val = float(it["value"])
            except (ValueError, TypeError):
                val = None

        if (min_val is not None or max_val is not None) and val is not None:
            violated = False
            reason = ""
            if min_val is not None and val < float(min_val):
                violated = True
                reason = (
                    f"Measured value {val} is below minimum allowed {min_val} {unit or ''}".strip()
                )
            elif max_val is not None and val > float(max_val):
                violated = True
                reason = (
                    f"Measured value {val} exceeds maximum allowed {max_val} {unit or ''}".strip()
                )

            if violated:
                measurement_violations.append(
                    {
                        "item_id": tid or it.get("label"),
                        "label": it.get("label") or t_def.get("label") or tid,
                        "measured_value": val,
                        "min_value": min_val,
                        "max_value": max_val,
                        "unit": unit,
                        "reason": reason,
                    }
                )

    strict_quality_gate = params.get("require_all_required", False)
    if strict_quality_gate and missing_required:
        raise ChecklistIncompleteError(
            f"Cannot complete checklist {checklist_id!r}: missing required items: {missing_required}"
        )

    completed = len(missing_required) == 0
    passed = completed and len(measurement_violations) == 0

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
            passed,
            json.dumps(params.get("raw") or {}),
        )

        # Graph node & edge
        cl_label = f"{_NODE_TYPE_CHECKLIST}:{checklist_id}"
        wo_label = f"WORK_ORDER:{work_order_id}"

        await assert_owner(conn, ns_uuid, _NODE_TYPE_CHECKLIST, _OWNER_ENGINE)
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

        # Advance BOM_LINE status to TESTED on successful checklist completion (Wave FT-1)
        tested_lines: list[str] = []
        if passed:
            raw_lines: list[str] = []
            if params.get("bom_line_id"):
                raw_lines.append(str(params["bom_line_id"]).strip())
            if params.get("bom_lines"):
                raw_lines.extend(str(x).strip() for x in params["bom_lines"] if str(x).strip())

            # Also check if work order has installed BOM lines via graph edges
            if not raw_lines:
                try:
                    edge_rows = await conn.fetch(
                        """
                        SELECT object_label FROM kg_edges
                        WHERE namespace_id = $1::uuid
                          AND subject_label = $2
                          AND predicate = 'installs'
                        """,
                        ns_uuid,
                        wo_label,
                    )
                    raw_lines.extend(r["object_label"] for r in edge_rows)
                except Exception as exc:
                    log.debug("Failed to query installs edges for work order: %s", exc)

            for bl_id in raw_lines:
                target_quote_id = params.get("quote_id")
                target_line_ref = params.get("line_ref")
                if not (target_quote_id and target_line_ref):
                    clean_id = bl_id[len("BOM_LINE:") :] if bl_id.startswith("BOM_LINE:") else bl_id
                    if ":" in clean_id:
                        target_quote_id, target_line_ref = clean_id.split(":", 1)
                    else:
                        try:
                            bl_row = await conn.fetchrow(
                                """
                                SELECT quote_id, line_ref FROM bom_line_content
                                WHERE namespace_id = $1::uuid
                                  AND (bom_line_label = $2 OR bom_line_label = 'BOM_LINE:' || $2 OR line_ref = $2)
                                LIMIT 1
                                """,
                                ns_uuid,
                                bl_id,
                            )
                            if bl_row:
                                target_quote_id = bl_row["quote_id"]
                                target_line_ref = bl_row["line_ref"]
                        except Exception as exc:
                            log.debug("Failed to query bom_line_content: %s", exc)

                if target_quote_id and target_line_ref:
                    try:
                        await update_bom_line_status(
                            conn,
                            ns_uuid,
                            writer_engine="field_tech",
                            quote_id=target_quote_id,
                            line_ref=target_line_ref,
                            status="TESTED",
                        )
                        tested_lines.append(f"BOM_LINE:{target_quote_id}:{target_line_ref}")
                    except Exception as exc:
                        log.warning("Could not update BOM_LINE status to TESTED: %s", exc)

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
    # items/raw are JSONB; this pool registers no jsonb codec, so the
    # RETURNING clause above hands back raw JSON strings for both, never
    # decoded dicts/lists -- found while fixing the named work_orders.py/
    # partner_view.py sites in the same PR (same file family, same shape).
    # Same isinstance-guarded decode as resources/field_schedule.py's own
    # checklists.items handling.
    res["items"] = json.loads(res["items"]) if isinstance(res["items"], str) else res["items"]
    res["raw"] = json.loads(res["raw"]) if isinstance(res["raw"], str) else dict(res["raw"] or {})
    res["missing_required"] = missing_required
    res["is_complete"] = completed
    res["tested_lines"] = tested_lines
    res["passed"] = passed
    res["measurement_violations"] = measurement_violations
    res["completed_count"] = completed_count
    res["total_count"] = total_count
    res["kind"] = kind
    res["memory_id"] = str(params.get("memory_id") or res["id"])
    return res


async def do_complete_checklist(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record completed items for a work order checklist and verify ISO9001 compliance."""
    return await do_submit_checklist(engine, params)
