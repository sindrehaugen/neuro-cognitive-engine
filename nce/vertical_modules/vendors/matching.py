"""
nce/vertical_modules/vendors/matching.py
=========================================
Contractor matching and ranking logic for Vendors Axis (Batch 102).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.vendors.matching")

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"


def load_match_weights() -> dict[str, float]:
    """Load contractor matching weights from contractor-match-weights.json."""
    weights_path = _CONFIG_DATA_DIR / "contractor-match-weights.json"
    if not weights_path.exists():
        return {
            "skill_weight": 0.4,
            "location_weight": 0.3,
            "load_weight": 0.1,
            "history_weight": 0.2,
        }
    with open(weights_path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: float(v) for k, v in data.items() if not k.startswith("_")}


def get_location_str(loc: Any) -> str | None:
    """Helper to extract a clean case-insensitive string from a location field."""
    if isinstance(loc, str):
        return loc.strip().lower()
    if isinstance(loc, dict):
        for k in ("city", "name", "address", "location"):
            val = loc.get(k)
            if isinstance(val, str):
                return val.strip().lower()
    return None


async def do_match_contractor(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Match and rank contractors for a given job.

    Params:
        namespace_id (str | UUID): active namespace UUID
        job (dict): job specifications, e.g. {"skills": ["dsp"], "location": "Oslo"}
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    job = params.get("job")
    if not isinstance(job, dict):
        raise ValueError("job parameter must be a dictionary")

    job_skills: list[str] = job.get("skills") or []
    job_loc_raw = job.get("location")
    job_loc = get_location_str(job_loc_raw)

    weights = load_match_weights()
    skill_w = weights.get("skill_weight", 0.4)
    loc_w = weights.get("location_weight", 0.3)
    load_w = weights.get("load_weight", 0.1)
    hist_w = weights.get("history_weight", 0.2)

    # Normalize weights so they sum to 1.0 (if present)
    total_w = skill_w + loc_w + load_w + hist_w
    if total_w > 0:
        skill_w /= total_w
        loc_w /= total_w
        load_w /= total_w
        hist_w /= total_w

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # 1. Fetch all contractors
        rows = await conn.fetch(
            """
            SELECT contractor_id, partner_scope_id, profile, rates, skills, availability, performance_score
            FROM contractor_profiles
            WHERE namespace_id = $1
            """,
            ns_uuid,
        )

        # 2. Fetch load counts from kg_edges (assigned_to predicate)
        load_rows = await conn.fetch(
            """
            SELECT object_label AS contractor_id, COUNT(*) AS active_load
            FROM kg_edges
            WHERE predicate = 'assigned_to' AND namespace_id = $1
            GROUP BY object_label
            """,
            ns_uuid,
        )
        load_map = {r["contractor_id"]: int(r["active_load"]) for r in load_rows}

        matches = []
        for row in rows:
            contractor_id = row["contractor_id"]

            # Parse json fields safely
            def parse_json(val: Any) -> Any:
                if isinstance(val, str):
                    return json.loads(val)
                return val

            profile = parse_json(row["profile"]) or {}
            rates = parse_json(row["rates"]) or {}
            skills = row["skills"] or []
            availability = parse_json(row["availability"]) or {}
            performance_score = row["performance_score"]

            # Compute Skill Score (graceful degradation if no job skills required)
            if not job_skills:
                skill_score = 1.0
            else:
                if not skills:
                    skill_score = 0.0
                else:
                    matched_skills = set(s.lower() for s in job_skills) & set(
                        s.lower() for s in skills
                    )
                    skill_score = len(matched_skills) / len(job_skills)

            # Compute Location Score
            if not job_loc:
                loc_score = 1.0
            else:
                c_loc_raw = (
                    profile.get("location") or availability.get("location") or profile.get("city")
                )
                c_loc = get_location_str(c_loc_raw)
                if c_loc and c_loc == job_loc:
                    loc_score = 1.0
                else:
                    loc_score = 0.0

            # Compute Load Score
            load_count = load_map.get(contractor_id, 0)
            load_score = 1.0 / (1.0 + load_count)

            # Compute History Score (neutral score 80.0/0.8 if missing)
            if performance_score is not None:
                hist_score = float(performance_score) / 100.0
            else:
                hist_score = 0.8

            # Calculate composite score
            score = (
                skill_score * skill_w
                + loc_score * loc_w
                + load_score * load_w
                + hist_score * hist_w
            )

            matches.append(
                {
                    "contractor_id": contractor_id,
                    "score": round(score, 4),
                    "skill_score": round(skill_score, 4),
                    "location_score": round(loc_score, 4),
                    "load_score": round(load_score, 4),
                    "history_score": round(hist_score, 4),
                    "profile": profile,
                    "rates": rates,
                    "skills": skills,
                    "availability": availability,
                    "performance_score": float(performance_score)
                    if performance_score is not None
                    else None,
                }
            )

        # Sort matches by score descending, then by contractor_id alphabetically for deterministic tie-breaks
        matches.sort(key=lambda x: (-x["score"], x["contractor_id"]))

        return {
            "ok": True,
            "matches": matches,
        }
