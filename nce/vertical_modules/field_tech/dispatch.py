"""
nce/vertical_modules/field_tech/dispatch.py
============================================
AI Dispatch Advisor for Module 12 (Field Tech Engine).

Matches a technician to a work order by:
  skill/cert × location × current-load × outcome-history

Weights read from ``nce/config_data/field-tech-dispatch-weights.json``.
Graceful degradation: if HR/Vendors profiles are sparse, degrades cleanly
to location + current availability.

Strict Tenant Predicate Discipline (Charter §4.4)
-------------------------------------------------
EVERY query against v3_cognitive_ledger / work_orders carries an explicit
WHERE namespace_id = $N::uuid predicate.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.field_tech.dispatch")

_WEIGHTS_PATH = (
    Path(__file__).resolve().parents[2] / "config_data" / "field-tech-dispatch-weights.json"
)

_DEFAULT_WEIGHTS: dict[str, float] = {
    "skill_cert_weight": 0.4,
    "location_weight": 0.25,
    "load_weight": 0.15,
    "history_weight": 0.2,
}


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


def _load_weights() -> dict[str, float]:
    if not _WEIGHTS_PATH.exists():
        return dict(_DEFAULT_WEIGHTS)
    try:
        data = json.loads(_WEIGHTS_PATH.read_text(encoding="utf-8"))
        return {
            "skill_cert_weight": float(
                data.get("skill_cert_weight", _DEFAULT_WEIGHTS["skill_cert_weight"])
            ),
            "location_weight": float(
                data.get("location_weight", _DEFAULT_WEIGHTS["location_weight"])
            ),
            "load_weight": float(data.get("load_weight", _DEFAULT_WEIGHTS["load_weight"])),
            "history_weight": float(data.get("history_weight", _DEFAULT_WEIGHTS["history_weight"])),
        }
    except Exception as exc:
        log.warning("Failed to load dispatch weights from %s: %s", _WEIGHTS_PATH, exc)
        return dict(_DEFAULT_WEIGHTS)


async def do_dispatch(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Rank candidate technicians for a work order by composite score.

    Parameters
    ----------
    engine:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - work_order_id: (required) work order identifier.
        - candidates: (optional) list of candidate dicts:
            [{"id": "tech-1", "kind": "employee"|"contractor", "name": "...",
              "skills": [...], "location_id": "loc-A"}]
        - required_skills: (optional) list of required skills.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    weights = _load_weights()
    w_skill = weights["skill_cert_weight"]
    w_loc = weights["location_weight"]
    w_load = weights["load_weight"]
    w_hist = weights["history_weight"]

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Fetch work order
        wo = await conn.fetchrow(
            """
            SELECT work_order_id, namespace_id, kind, location_id, raw
            FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            """,
            work_order_id,
            ns_uuid,
        )
        if wo is None:
            raise ValueError(f"Work order {work_order_id!r} not found in namespace")

        wo_location = wo["location_id"]
        wo_raw = wo["raw"] or {}
        if isinstance(wo_raw, str):
            try:
                wo_raw = json.loads(wo_raw)
            except Exception:
                wo_raw = {}

        req_skills: list[str] = list(
            params.get("required_skills") or wo_raw.get("required_skills") or []
        )

        # 2. Get candidates (from params or discovered from graph / known tech nodes)
        provided_candidates = params.get("candidates")
        candidates: list[dict[str, Any]] = []

        if provided_candidates and isinstance(provided_candidates, list):
            candidates = list(provided_candidates)
        else:
            # Query candidate technicians from kg_nodes
            node_rows = await conn.fetch(
                """
                SELECT label, entity_type
                FROM kg_nodes
                WHERE namespace_id = $1::uuid
                  AND entity_type IN ('EMPLOYEE', 'CONTRACTOR')
                LIMIT 50
                """,
                ns_uuid,
            )
            for nr in node_rows:
                lbl = nr["label"]
                # e.g. EMPLOYEE:tech-1 or CONTRACTOR:cont-2
                cid = lbl.split(":", 1)[1] if ":" in lbl else lbl
                candidates.append(
                    {
                        "id": cid,
                        "kind": nr["entity_type"].lower(),
                        "name": cid,
                        "skills": [],
                        "location_id": None,
                    }
                )

        if not candidates:
            # Graceful fallback: return empty candidate ranking with explanation
            return {
                "work_order_id": work_order_id,
                "ranked_candidates": [],
                "rationale": "No eligible internal technicians or external contractors found in pool.",
            }

        # 3. Query current load for each candidate (active WOs in this namespace)
        candidate_ids = [str(c["id"]) for c in candidates]
        load_rows = await conn.fetch(
            """
            SELECT assignee_id, COUNT(*) as open_count
            FROM work_orders
            WHERE namespace_id = $1::uuid
              AND status IN ('assigned', 'in_progress')
              AND assignee_id = ANY($2::text[])
            GROUP BY assignee_id
            """,
            ns_uuid,
            candidate_ids,
        )
        load_map = {lr["assignee_id"]: int(lr["open_count"]) for lr in load_rows}

        # 4. Query outcome history from v3_cognitive_ledger for each candidate
        history_rows = await conn.fetch(
            """
            SELECT
                tlx_scores->>'completed_by' as completed_by,
                COUNT(*) as completed_count,
                AVG(COALESCE((tlx_scores->>'rating')::numeric, 5.0)) as avg_rating,
                AVG(COALESCE((tlx_scores->>'quality_score')::numeric, 1.0)) as avg_quality
            FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND tlx_scores->>'event_type' = 'field_tech_outcome'
              AND tlx_scores->>'completed_by' = ANY($2::text[])
            GROUP BY tlx_scores->>'completed_by'
            """,
            ns_uuid,
            candidate_ids,
        )
        hist_map: dict[str, dict[str, Any]] = {}
        for hr in history_rows:
            hist_map[hr["completed_by"]] = {
                "count": int(hr["completed_count"]),
                "avg_rating": float(hr["avg_rating"]),
                "avg_quality": float(hr["avg_quality"]),
            }

    # 5. Pure scoring over candidates
    scored_candidates = []
    for c in candidates:
        cid = str(c["id"])
        c_skills = set(c.get("skills") or [])
        c_loc = c.get("location_id")

        # Skill score
        if not req_skills:
            skill_score = 1.0
        else:
            matched = sum(1 for s in req_skills if s in c_skills)
            skill_score = matched / len(req_skills)

        # Location score
        if not wo_location or not c_loc:
            loc_score = 0.8  # Default baseline
        elif c_loc == wo_location:
            loc_score = 1.0
        else:
            loc_score = 0.4

        # Load score (1.0 for 0 active jobs, decaying with load)
        active_load = load_map.get(cid, 0)
        load_score = 1.0 / (1.0 + 0.3 * active_load)

        # History score
        h_info = hist_map.get(cid)
        if h_info and h_info["count"] > 0:
            # Scale rating [1..5] to [0.2..1.0] and quality [0..1]
            rating_norm = max(0.0, min(1.0, h_info["avg_rating"] / 5.0))
            hist_score = 0.6 * rating_norm + 0.4 * max(0.0, min(1.0, h_info["avg_quality"]))
        else:
            hist_score = 0.7  # Neutral prior for newcomers

        # Composite score
        total_score = (
            w_skill * skill_score + w_loc * loc_score + w_load * load_score + w_hist * hist_score
        )

        rationale = (
            f"Skill match: {skill_score:.2f}, Location: {loc_score:.2f}, "
            f"Active load: {active_load} ({load_score:.2f}), "
            f"History: {hist_score:.2f} (from {h_info['count'] if h_info else 0} prior jobs)"
        )

        scored_candidates.append(
            {
                "id": cid,
                "kind": c.get("kind", "employee"),
                "name": c.get("name", cid),
                "score": round(total_score, 4),
                "component_scores": {
                    "skill": round(skill_score, 4),
                    "location": round(loc_score, 4),
                    "load": round(load_score, 4),
                    "history": round(hist_score, 4),
                },
                "active_load": active_load,
                "rationale": rationale,
            }
        )

    # Sort descending by score
    scored_candidates.sort(key=lambda x: x["score"], reverse=True)

    top_tech = scored_candidates[0]["name"] if scored_candidates else "None"
    return {
        "work_order_id": work_order_id,
        "weights": weights,
        "ranked_candidates": scored_candidates,
        "top_recommendation": top_tech,
        "rationale": f"Top candidate {top_tech!r} selected by composite skill/location/load/history recall.",
    }
