"""
nce.vertical_modules.resources.planner
======================================
AI Allocation Planner for Module 15 (Staff & Resources Engine).
Provides cognitive recall from v3_cognitive_ledger (Spec §79), multi-objective
candidate scoring (skills, travel, load, internal preference, historical outcomes),
and Tiered Autonomy gating against NCE_RESOURCES_AUTONOMY_ALLOCATION_CEILING (Spec §78).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import _parse_datetime, do_reserve
from nce.vertical_modules.resources.registry import _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.planner")

WEIGHTS_FILE = Path(__file__).parent / "resources-allocation-weights.json"


def load_allocation_weights() -> dict[str, float]:
    """Load default weights from resources-allocation-weights.json."""
    if WEIGHTS_FILE.exists():
        try:
            with open(WEIGHTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {
                "skill_match_weight": float(data.get("skill_match_weight", 0.35)),
                "travel_distance_weight": float(data.get("travel_distance_weight", 0.20)),
                "load_balance_weight": float(data.get("load_balance_weight", 0.15)),
                "internal_preference_weight": float(data.get("internal_preference_weight", 0.15)),
                "outcome_history_weight": float(data.get("outcome_history_weight", 0.15)),
            }
        except Exception as exc:
            log.warning("Failed to parse %s, using defaults: %s", WEIGHTS_FILE, exc)

    return {
        "skill_match_weight": 0.35,
        "travel_distance_weight": 0.20,
        "load_balance_weight": 0.15,
        "internal_preference_weight": 0.15,
        "outcome_history_weight": 0.15,
    }


async def do_plan_allocation(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Plan resource allocation for a demand.
    Scores candidates via skills, location, load, internal/contractor preference,
    and cognitive recall from v3_cognitive_ledger.
    Enforces tiered autonomy: auto-reserves if value <= ceiling & auto_reserve=True,
    otherwise recommends as Advisor with requires_approval=True.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    demand_kind = str(params.get("demand_kind") or "project").strip()
    demand_id_raw = params.get("demand_id")
    demand_id = _parse_uuid(demand_id_raw, "demand_id") if demand_id_raw else None

    starts_at = _parse_datetime(params.get("starts_at"), "starts_at")
    ends_at = _parse_datetime(params.get("ends_at"), "ends_at")
    if ends_at <= starts_at:
        raise ResourceValidationError(f"ends_at ({ends_at}) must be after starts_at ({starts_at}).")

    required_skills = [str(s).strip().lower() for s in params.get("required_skills") or [] if s]
    required_kinds = params.get("required_kinds") or ["employee"]
    if isinstance(required_kinds, str):
        required_kinds = [required_kinds]

    demand_location = str(params.get("location") or "").strip().lower()
    estimated_value_nok = float(params.get("estimated_value_nok", 0.0))
    auto_reserve = bool(params.get("auto_reserve", False))

    weights = load_allocation_weights()
    if isinstance(params.get("tenant_weights"), dict):
        weights.update(params["tenant_weights"])

    w_skill = weights["skill_match_weight"]
    w_dist = weights["travel_distance_weight"]
    w_load = weights["load_balance_weight"]
    w_pref = weights["internal_preference_weight"]
    w_hist = weights["outcome_history_weight"]

    pool = _extract_pool(engine)
    selected_plan: list[dict[str, Any]] = []

    async with scoped_pg_session(pool, ns_id) as conn:
        for kind in required_kinds:
            # 1. Fetch candidates for this kind
            candidates_rows = await conn.fetch(
                """
                SELECT id, kind, ref_id, display_name, attrs
                FROM resources
                WHERE namespace_id = $1 AND kind = $2
                """,
                ns_id,
                kind,
            )

            # 2. Check each candidate for existing conflicting allocation during window
            eligible_candidates = []
            for r in candidates_rows:
                conflict = await conn.fetchval(
                    """
                    SELECT 1 FROM allocations
                    WHERE namespace_id = $1
                      AND resource_id = $2
                      AND status <> 'released'
                      AND tstzrange(starts_at, ends_at) && tstzrange($3, $4)
                    LIMIT 1
                    """,
                    ns_id,
                    r["id"],
                    starts_at,
                    ends_at,
                )
                if not conflict:
                    eligible_candidates.append(r)

            if not eligible_candidates:
                continue

            # 3. For each eligible candidate, compute multi-objective score
            scored: list[dict[str, Any]] = []
            for cand in eligible_candidates:
                cand_id = str(cand["id"])
                raw_attrs = cand["attrs"]
                if isinstance(raw_attrs, str):
                    cand_attrs = json.loads(raw_attrs)
                elif isinstance(raw_attrs, dict):
                    cand_attrs = raw_attrs
                else:
                    cand_attrs = {}

                # a. Skill match score
                cand_skills = [str(s).strip().lower() for s in cand_attrs.get("skills", [])]
                if required_skills:
                    matched_count = sum(1 for s in required_skills if s in cand_skills)
                    s_skill = matched_count / len(required_skills)
                else:
                    s_skill = 1.0

                # b. Location / travel distance score
                cand_loc = str(cand_attrs.get("base_location") or "").strip().lower()
                if not demand_location or not cand_loc:
                    s_dist = 0.8
                elif demand_location in cand_loc or cand_loc in demand_location:
                    s_dist = 1.0
                else:
                    s_dist = 0.5

                # c. Current load score (allocations in ±7 days)
                open_alloc_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM allocations
                    WHERE namespace_id = $1
                      AND resource_id = $2
                      AND status <> 'released'
                      AND starts_at >= $3 - INTERVAL '7 days'
                      AND ends_at <= $4 + INTERVAL '7 days'
                    """,
                    ns_id,
                    cand["id"],
                    starts_at,
                    ends_at,
                )
                open_alloc_count = open_alloc_count or 0
                s_load = max(0.1, 1.0 - (float(open_alloc_count) * 0.1))

                # d. Internal preference (employees preferred over external contractors)
                s_pref = 1.0 if cand["kind"] != "contractor" else 0.6

                # e. Cognitive recall from v3_cognitive_ledger
                hist_row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS total_jobs,
                           AVG(COALESCE((tlx_scores->>'rating')::numeric, 5.0)) AS avg_rating,
                           AVG(COALESCE((tlx_scores->>'quality_score')::numeric, 1.0)) AS avg_quality
                    FROM v3_cognitive_ledger
                    WHERE namespace_id = $1
                      AND tlx_scores->>'resource_id' = $2
                    """,
                    ns_id,
                    cand_id,
                )
                if hist_row and hist_row["total_jobs"] and hist_row["total_jobs"] > 0:
                    hist_rating = float(hist_row["avg_rating"]) / 5.0
                    hist_qual = float(hist_row["avg_quality"])
                    s_hist = (hist_rating + hist_qual) / 2.0
                    cognitive_recall = {
                        "historical_jobs": int(hist_row["total_jobs"]),
                        "avg_rating": float(hist_row["avg_rating"]),
                        "avg_quality": float(hist_row["avg_quality"]),
                    }
                else:
                    s_hist = 0.8  # neutral baseline for unobserved resources
                    cognitive_recall = {
                        "historical_jobs": 0,
                        "avg_rating": None,
                        "avg_quality": None,
                    }

                composite = (
                    (w_skill * s_skill)
                    + (w_dist * s_dist)
                    + (w_load * s_load)
                    + (w_pref * s_pref)
                    + (w_hist * s_hist)
                )

                scored.append(
                    {
                        "resource_id": cand_id,
                        "kind": cand["kind"],
                        "display_name": cand["display_name"],
                        "composite_score": round(composite, 4),
                        "score_breakdown": {
                            "skill_match": round(s_skill, 2),
                            "travel_distance": round(s_dist, 2),
                            "load_balance": round(s_load, 2),
                            "internal_preference": round(s_pref, 2),
                            "outcome_history": round(s_hist, 2),
                        },
                        "cognitive_recall": cognitive_recall,
                    }
                )

            if scored:
                scored.sort(key=lambda x: x["composite_score"], reverse=True)
                winner = scored[0]
                winner["required_kind"] = kind
                selected_plan.append(winner)

    # 4. Tiered Autonomy Evaluation
    ceiling = cfg.NCE_RESOURCES_AUTONOMY_ALLOCATION_CEILING
    sub_threshold = estimated_value_nok <= ceiling

    if sub_threshold and auto_reserve and len(selected_plan) == len(required_kinds):
        # Auto-crew sub-threshold jobs (Actor Mode)
        created_allocations = []
        for winner in selected_plan:
            alloc_res = await do_reserve(
                engine,
                {
                    "namespace_id": ns_id,
                    "resource_id": winner["resource_id"],
                    "demand_kind": demand_kind,
                    "demand_id": demand_id,
                    "starts_at": starts_at.isoformat(),
                    "ends_at": ends_at.isoformat(),
                    "confidence": winner["composite_score"],
                    "attrs": {
                        "planned_by": "ai_planner_v1",
                        "score_breakdown": winner["score_breakdown"],
                        "estimated_value_nok": estimated_value_nok,
                    },
                },
            )
            created_allocations.append(alloc_res)

        return {
            "status": "reserved",
            "autonomous": True,
            "requires_approval": False,
            "estimated_value_nok": estimated_value_nok,
            "ceiling": ceiling,
            "plan": selected_plan,
            "allocations": created_allocations,
            "rationale": f"Job value ({estimated_value_nok:,.0f} NOK) is within autonomy ceiling ({ceiling:,.0f} NOK). Auto-reserved successfully.",
        }

    # Over-threshold or manual review required (Advisor Mode)
    reason = (
        f"Estimated value ({estimated_value_nok:,.0f} NOK) exceeds autonomy ceiling ({ceiling:,.0f} NOK). Human approval required."
        if not sub_threshold
        else "Plan formulated as recommendation (auto_reserve was False)."
    )

    return {
        "status": "suggested",
        "autonomous": False,
        "requires_approval": True,
        "estimated_value_nok": estimated_value_nok,
        "ceiling": ceiling,
        "plan": selected_plan,
        "rationale": reason,
    }


async def do_record_allocation_outcome(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Append an allocation outcome to v3_cognitive_ledger for cognitive recall feedback loop (Spec §79).
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    res_id = _parse_uuid(params.get("resource_id"), "resource_id")
    alloc_id_raw = params.get("allocation_id")
    alloc_id = _parse_uuid(alloc_id_raw, "allocation_id") if alloc_id_raw else None

    rating = float(params.get("rating", 5.0))
    if not (1.0 <= rating <= 5.0):
        raise ResourceValidationError("rating must be between 1.0 and 5.0.")

    quality_score = float(params.get("quality_score", 1.0))
    if not (0.0 <= quality_score <= 1.0):
        raise ResourceValidationError("quality_score must be between 0.0 and 1.0.")

    tlx = {
        "event_type": "resource_allocation_outcome",
        "resource_id": str(res_id),
        "allocation_id": str(alloc_id) if alloc_id else None,
        "demand_kind": str(params.get("demand_kind") or "project"),
        "rating": rating,
        "quality_score": quality_score,
        "on_time": bool(params.get("on_time", True)),
        "notes": str(params.get("notes") or ""),
    }

    pool = _extract_pool(engine)
    ledger_id = uuid4()
    async with scoped_pg_session(pool, ns_id) as conn:
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                id, namespace_id, empathic_tensor, tlx_scores, model_version
            )
            VALUES ($1, $2, '[0.0, 0.0, 0.0, 0.0, 0.0, 0.0]'::vector, $3::jsonb, $4)
            """,
            ledger_id,
            ns_id,
            json.dumps(tlx),
            "m15_resources_planner_v1",
        )

    return {
        "ledger_id": str(ledger_id),
        "namespace_id": str(ns_id),
        "resource_id": str(res_id),
        "rating": rating,
        "quality_score": quality_score,
    }
