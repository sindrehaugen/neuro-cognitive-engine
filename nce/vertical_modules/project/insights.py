"""
nce/vertical_modules/project/insights.py
=========================================
Domain core: Watcher (do_detect_scope_creep) and Advisor (do_status_report) cores.

Features:
1. do_detect_scope_creep(engine, params) -> dict:
   Diff current BOM against Sales-frozen baseline via CHANGE_ORDER vs baseline.
2. do_status_report(engine, params) -> dict:
   Generate retrieval-grounded narrative + margin-trinity snapshot.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.structural.grounded import ground
from nce.vertical_modules.project.baseline import _read_signed_baseline, build_margin_trinity

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.insights")


def _quote_id_from_project_label(project_label: str) -> str:
    """Extract the quote_id fragment from a PROJECT:{QUOTE} label."""
    prefix = "PROJECT:"
    if not project_label.upper().startswith(prefix):
        raise ValueError(f"project_label {project_label!r} does not start with 'PROJECT:'")
    return project_label[len(prefix) :].strip()


async def do_detect_scope_creep(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Diff current BOM against Sales-frozen baseline via CHANGE_ORDER vs baseline.

    Parameters
    ----------
    engine:
        NCEEngine instance.
    params:
        {
            "namespace_id": str | UUID,   # required
            "project_id":   str,          # required — e.g. "PROJECT:XYZ"
        }
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_detect_scope_creep: 'namespace_id' is required"}
    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except (ValueError, AttributeError) as exc:
        return {"ok": False, "error": f"do_detect_scope_creep: invalid namespace_id: {exc}"}

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return {"ok": False, "error": "do_detect_scope_creep: 'project_id' is required"}

    try:
        quote_id = _quote_id_from_project_label(project_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Read Sales-frozen baseline total value via A2A seam
    baseline_row = None
    sales_available = False
    try:
        baseline_row = await _read_signed_baseline(engine, ns_uuid, quote_id)
        if baseline_row is not None:
            sales_available = True
    except NotImplementedError:
        log.info(
            "do_detect_scope_creep: Sales engine not available for quote=%s — degraded",
            quote_id,
        )
    except Exception as exc:
        log.warning(
            "do_detect_scope_creep: A2A baseline read failed for quote=%s: %s",
            quote_id,
            exc,
        )

    change_orders_list: list[dict[str, Any]] = []
    total_creep_value = 0.0

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Query CHANGE_ORDER nodes amending BOM lines contained in this project
        rows = await conn.fetch(
            """
            SELECT DISTINCT n.label, n.id
            FROM kg_nodes n
            JOIN kg_edges e_amends 
                 ON e_amends.subject_label = n.label 
                AND e_amends.namespace_id = n.namespace_id
            JOIN kg_edges e_contains 
                 ON e_contains.object_label = e_amends.object_label 
                AND e_contains.namespace_id = n.namespace_id
            WHERE n.entity_type = 'PROJECT_CHANGE_ORDER'
              AND e_amends.predicate = 'amends'
              AND e_contains.predicate = 'contains'
              AND e_contains.subject_label = $1
              AND n.namespace_id = $2::uuid
            ORDER BY n.label
            """,
            project_id,
            str(ns_uuid),
        )

        for r in rows:
            co_label = r["label"]
            co_id = r["id"]

            # Fetch has_value edge
            val_row = await conn.fetchrow(
                """
                SELECT object_label 
                FROM kg_edges 
                WHERE subject_label = $1 
                  AND predicate = 'has_value' 
                  AND namespace_id = $2::uuid 
                LIMIT 1
                """,
                co_label,
                str(ns_uuid),
            )

            # Fetch amends edge
            amends_row = await conn.fetchrow(
                """
                SELECT object_label 
                FROM kg_edges 
                WHERE subject_label = $1 
                  AND predicate = 'amends' 
                  AND namespace_id = $2::uuid 
                LIMIT 1
                """,
                co_label,
                str(ns_uuid),
            )

            co_value = 0.0
            if val_row:
                val_str = val_row["object_label"]
                if val_str.upper().startswith("VALUE:"):
                    val_str = val_str[6:].strip()
                try:
                    co_value = float(val_str)
                except ValueError:
                    co_value = 0.0

            total_creep_value += co_value

            change_orders_list.append(
                {
                    "label": co_label,
                    "node_id": str(co_id),
                    "value": co_value,
                    "amended_bom_line": amends_row["object_label"] if amends_row else None,
                }
            )

    signed_total_nok = 0.0
    if sales_available and baseline_row:
        signed_total_nok = float(baseline_row.get("signed_total_nok", 0.0))

    current_total_nok = signed_total_nok + total_creep_value

    return {
        "ok": True,
        "change_orders": change_orders_list,
        "delta_signed_vs_current": total_creep_value,
        "signed_total_nok": signed_total_nok if sales_available else None,
        "current_total_nok": current_total_nok if sales_available else total_creep_value,
        "sales_available": sales_available,
    }


async def do_status_report(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Generate status report narrative and margin-trinity snapshot.

    Parameters
    ----------
    engine:
        NCEEngine instance.
    params:
        {
            "namespace_id":          str | UUID,   # required
            "project_id":            str,          # required — e.g. "PROJECT:XYZ"
            "estimated_cost_nok":    float,        # optional
            "estimated_revenue_nok": float,        # optional
        }
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_status_report: 'namespace_id' is required"}
    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except (ValueError, AttributeError) as exc:
        return {"ok": False, "error": f"do_status_report: invalid namespace_id: {exc}"}

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return {"ok": False, "error": "do_status_report: 'project_id' is required"}

    try:
        quote_id = _quote_id_from_project_label(project_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Fetch Sales baseline to extract defaults for trinity
    baseline_row = None
    try:
        baseline_row = await _read_signed_baseline(engine, ns_uuid, quote_id)
    except Exception:
        pass

    signed_total = 0.0
    signed_margin = 0.0
    if baseline_row:
        signed_total = float(baseline_row.get("signed_total_nok", 0.0))
        signed_margin = float(baseline_row.get("signed_margin_pct", 0.0))

    signed_cost = signed_total * (1.0 - signed_margin)

    # Determine estimates (defaulting to baseline figures if absent)
    est_cost = float(params.get("estimated_cost_nok") or signed_cost)
    est_rev = float(params.get("estimated_revenue_nok") or signed_total)

    # Resolve margin trinity
    trinity_params = {
        "namespace_id": ns_uuid,
        "quote_id": quote_id,
        "estimated_cost_nok": est_cost,
        "estimated_revenue_nok": est_rev,
    }
    trinity = await build_margin_trinity(engine, trinity_params)

    # Fetch current gate and entered timestamp
    current_phase = "unknown"
    dwell_days = 0

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT e.object_label, n.created_at
            FROM kg_edges e
            JOIN kg_nodes n ON n.label = e.object_label AND n.namespace_id = e.namespace_id
            WHERE e.subject_label = $1
              AND e.predicate = 'in_phase'
              AND e.namespace_id = $2::uuid
            LIMIT 1
            """,
            project_id,
            str(ns_uuid),
        )

        if row:
            gate_label = row["object_label"]
            gate_created_at = row["created_at"]
            if ":" in gate_label:
                current_phase = gate_label.split(":")[-1]
            else:
                current_phase = gate_label

            if gate_created_at:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                dwell_days = max(0, (now_utc - gate_created_at).days)

        # Retrieve scope creep delta
        creep_res = await do_detect_scope_creep(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": project_id,
            },
        )
        delta = creep_res.get("delta_signed_vs_current", 0.0)

        # Clear stale status fact nodes
        await conn.execute(
            """
            DELETE FROM kg_nodes
            WHERE namespace_id = $1::uuid
              AND (label LIKE $2 OR label LIKE $3 OR label LIKE $4)
            """,
            str(ns_uuid),
            f"PROJECT:{quote_id}:STATUS:MARGIN:%",
            f"PROJECT:{quote_id}:STATUS:GATE:%",
            f"PROJECT:{quote_id}:STATUS:CREEP:%",
        )

        # Format values for labels
        def fmt_margin(v: Any) -> str:
            if v is None:
                return "None"
            if isinstance(v, (int, float)):
                return f"{float(v) * 100:.1f}%"
            return str(v)

        margin_fact = f"Margin trinity: signed={fmt_margin(trinity.get('signed'))}, estimated={fmt_margin(trinity.get('estimated'))}, actual={fmt_margin(trinity.get('actual'))}"
        margin_label = f"PROJECT:{quote_id}:STATUS:MARGIN:{margin_fact}"

        gate_fact = f"Gate dwell: current phase is {current_phase} (dwell: {dwell_days} days)"
        gate_fact_label = f"PROJECT:{quote_id}:STATUS:GATE:{gate_fact}"

        creep_fact = f"Scope creep delta: {delta:,.1f} NOK"
        creep_label = f"PROJECT:{quote_id}:STATUS:CREEP:{creep_fact}"

        # Assert owner-engine for project-owned nodes before inserting
        await assert_owner(conn, ns_uuid, "PROJECT_TASK", "project")

        # Insert fact nodes
        margin_uuid = await conn.fetchval(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'PROJECT_TASK', $2::uuid)
            ON CONFLICT (label, namespace_id) DO UPDATE SET updated_at = NOW()
            RETURNING id
            """,
            margin_label,
            str(ns_uuid),
        )

        gate_uuid = await conn.fetchval(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'PROJECT_TASK', $2::uuid)
            ON CONFLICT (label, namespace_id) DO UPDATE SET updated_at = NOW()
            RETURNING id
            """,
            gate_fact_label,
            str(ns_uuid),
        )

        creep_uuid = await conn.fetchval(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, 'PROJECT_TASK', $2::uuid)
            ON CONFLICT (label, namespace_id) DO UPDATE SET updated_at = NOW()
            RETURNING id
            """,
            creep_label,
            str(ns_uuid),
        )

        # Call C9a grounded helper to build the report prose
        claims: list[dict[str, str | UUID]] = [
            {"node_id": str(margin_uuid)},
            {"node_id": str(gate_uuid)},
            {"node_id": str(creep_uuid)},
        ]
        template = "Project Status Report: {facts}"

        ground_res = await ground(
            conn,
            namespace_id=ns_uuid,
            claims=claims,
            template=template,
        )

        return {
            "ok": True,
            "narrative": ground_res["prose"],
            "margin_trinity": trinity,
            "citations": ground_res["citations"],
        }
