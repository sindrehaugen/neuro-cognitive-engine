"""
nce/vertical_modules/hr/skills.py
=================================
Skills matrix, certification tracking, and requirement matching for Module 13.

Functions:
  - do_record_skill: Assign or update an employee skill.
  - do_record_certification: Register or update an employee certification.
  - do_match_skills: Evaluate employee fit against specific project/WO requirements.

RL-1 Invariant:
  - NEVER ranking: Refuses leaderboards, standing employee scores, and comparative peer ranks.
  - Returns requirement satisfaction per person for an eligible set.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.hr._guard import assert_ranking_prohibited
from nce.vertical_modules.hr.taxonomy import resolve_implied_skills

log = logging.getLogger("nce.vertical_modules.hr.skills")


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, name: str) -> UUID:
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val).strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid UUID for {name}: {val!r}") from exc


async def do_record_skill(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record or update a technical skill on an employee profile.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Target employee ID.
        - skill_id: (required) Unique skill ID or slug (e.g. 'dante-routing').
        - name: (required) Skill display name.
        - category: (optional, default 'general') e.g. audio, video, control, network.
        - level: (optional, default 'intermediate') beginner, intermediate, advanced, expert.
        - hr_source_id: (optional) Upstream source ID for GDPR hard retirement.
        - raw: (optional) Metadata dict.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    skill_id = str(params.get("skill_id") or "").strip()
    if not skill_id:
        raise ValueError("skill_id is required")

    name = str(params.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")

    category = str(params.get("category") or "general").strip()
    level = str(params.get("level") or "intermediate").strip()
    hr_source_id = params.get("hr_source_id")
    raw = params.get("raw") or {}
    raw_json = json.dumps(raw) if not isinstance(raw, str) else raw

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO skills (
                skill_id, employee_id, namespace_id, name, category, level,
                raw, hr_source_id, assessed_at
            )
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7::jsonb, $8, now())
            ON CONFLICT (employee_id, skill_id, namespace_id) DO UPDATE
            SET name = EXCLUDED.name,
                category = EXCLUDED.category,
                level = EXCLUDED.level,
                raw = EXCLUDED.raw,
                hr_source_id = COALESCE(EXCLUDED.hr_source_id, skills.hr_source_id),
                assessed_at = now(),
                updated_at = now()
            RETURNING id, skill_id, employee_id, namespace_id, name, category, level,
                      assessed_at, hr_source_id
            """,
            skill_id,
            employee_id,
            ns_uuid,
            name,
            category,
            level,
            raw_json,
            hr_source_id,
        )

    return {
        "id": str(row["id"]),
        "skill_id": row["skill_id"],
        "employee_id": row["employee_id"],
        "namespace_id": str(row["namespace_id"]),
        "name": row["name"],
        "category": row["category"],
        "level": row["level"],
        "assessed_at": row["assessed_at"].isoformat() if row["assessed_at"] else None,
        "hr_source_id": row["hr_source_id"],
    }


async def do_record_certification(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record or update a vendor/industry certification for an employee.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Target employee ID.
        - cert_id: (required) Certification ID or license number.
        - authority: (required) Issuing body (e.g. 'AVIXA', 'Crestron', 'QSC').
        - name: (required) Certification name (e.g. 'CTS-D', 'Q-SYS Level 2').
        - issued: (required) Issuance ISO timestamp or date.
        - valid_to: (optional) Expiration ISO timestamp or date.
        - status: (optional, default 'active') active, expired, pending_renewal.
        - hr_source_id: (optional) Upstream source ID for GDPR hard retirement.
        - raw: (optional) Metadata dict.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    cert_id = str(params.get("cert_id") or "").strip()
    if not cert_id:
        raise ValueError("cert_id is required")

    authority = str(params.get("authority") or "").strip()
    if not authority:
        raise ValueError("authority is required")

    name = str(params.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")

    issued = params.get("issued")
    if not issued:
        from datetime import date

        issued = date.today().isoformat()

    valid_to = params.get("valid_to")
    status = str(params.get("status") or "active").strip()
    hr_source_id = params.get("hr_source_id")
    raw = params.get("raw") or {}
    raw_json = json.dumps(raw) if not isinstance(raw, str) else raw

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO certifications (
                cert_id, employee_id, namespace_id, authority, name,
                issued, valid_to, status, raw, hr_source_id
            )
            VALUES ($1, $2, $3::uuid, $4, $5, $6::timestamptz, $7::timestamptz, $8, $9::jsonb, $10)
            ON CONFLICT (cert_id, namespace_id) DO UPDATE
            SET authority = EXCLUDED.authority,
                name = EXCLUDED.name,
                issued = EXCLUDED.issued,
                valid_to = EXCLUDED.valid_to,
                status = EXCLUDED.status,
                raw = EXCLUDED.raw,
                hr_source_id = COALESCE(EXCLUDED.hr_source_id, certifications.hr_source_id),
                updated_at = now()
            RETURNING id, cert_id, employee_id, namespace_id, authority, name,
                      issued, valid_to, status, hr_source_id
            """,
            cert_id,
            employee_id,
            ns_uuid,
            authority,
            name,
            issued,
            valid_to,
            status,
            raw_json,
            hr_source_id,
        )

    return {
        "id": str(row["id"]),
        "cert_id": row["cert_id"],
        "employee_id": row["employee_id"],
        "namespace_id": str(row["namespace_id"]),
        "authority": row["authority"],
        "name": row["name"],
        "issued": row["issued"].isoformat() if row["issued"] else None,
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "status": row["status"],
        "hr_source_id": row["hr_source_id"],
    }


async def do_match_skills(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Match candidates against specific job/project skill and cert requirements.

    Enforces RL-1:
      - NEVER ranking: Returns requirement fit per person for an eligible set.
      - Refuses requests that attempt to construct leaderboards or cross-person rankings.
      - Resolves implied skills from certifications held.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - required_skills: (optional) List of required skill names.
        - required_certs: (optional) List of required certification names.
        - candidates: (optional) List of candidate profile dicts. If omitted, active
          employees in the namespace are fetched.
    """
    # 1. Enforce RL-1 policy before touching data
    assert_ranking_prohibited(params)

    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    req_skills = [str(s).strip() for s in (params.get("required_skills") or []) if str(s).strip()]
    req_certs = [str(c).strip() for c in (params.get("required_certs") or []) if str(c).strip()]

    candidates_input = params.get("candidates")
    candidate_profiles: list[dict[str, Any]] = []

    if candidates_input and isinstance(candidates_input, list):
        for c in candidates_input:
            candidate_profiles.append(
                {
                    "employee_id": str(c.get("employee_id") or c.get("id") or ""),
                    "name": str(c.get("name") or ""),
                    "skills": list(c.get("skills") or []),
                    "certs": list(c.get("certs") or c.get("certifications") or []),
                }
            )
    else:
        # Load from DB with explicit tenant predicates
        pool = _extract_pool(engine)
        async with scoped_pg_session(pool, ns_uuid) as conn:
            emp_rows = await conn.fetch(
                """
                SELECT employee_id, name
                FROM   employees
                WHERE  namespace_id = $1::uuid AND active = true
                LIMIT 100
                """,
                ns_uuid,
            )
            for er in emp_rows:
                emp_id = er["employee_id"]
                s_rows = await conn.fetch(
                    "SELECT name FROM skills WHERE employee_id = $1 AND namespace_id = $2::uuid",
                    emp_id,
                    ns_uuid,
                )
                c_rows = await conn.fetch(
                    """
                    SELECT name FROM certifications
                    WHERE employee_id = $1 AND namespace_id = $2::uuid
                      AND status = 'active'
                      AND (valid_to IS NULL OR valid_to > now())
                    """,
                    emp_id,
                    ns_uuid,
                )
                candidate_profiles.append(
                    {
                        "employee_id": emp_id,
                        "name": er["name"],
                        "skills": [sr["name"] for sr in s_rows],
                        "certs": [cr["name"] for cr in c_rows],
                    }
                )

    # Pure requirement fit evaluation
    eligible_set: list[dict[str, Any]] = []
    for cand in candidate_profiles:
        c_emp_id = cand["employee_id"]
        c_name = cand["name"]

        # Explicit skills + implied skills from held certs
        c_skills = set(cand["skills"])
        c_certs = set(cand["certs"])
        implied_skills = resolve_implied_skills(list(c_certs))
        effective_skills = c_skills | implied_skills

        # Check skills
        matched_skills = [
            s for s in req_skills if any(s.lower() == cs.lower() for cs in effective_skills)
        ]
        missing_skills = [
            s for s in req_skills if not any(s.lower() == cs.lower() for cs in effective_skills)
        ]

        # Check certs
        matched_certs = [c for c in req_certs if any(c.lower() == cc.lower() for cc in c_certs)]
        missing_certs = [c for c in req_certs if not any(c.lower() == cc.lower() for cc in c_certs)]

        # Eligibility: must hold all mandatory certs and have zero missing mandatory certs
        is_eligible = (len(missing_certs) == 0) and (
            len(missing_skills) == 0 if req_skills else True
        )

        # Plain language rationale explaining individual fit
        rationale_parts = []
        if matched_certs:
            rationale_parts.append(f"Holds required certs: {', '.join(matched_certs)}")
        if missing_certs:
            rationale_parts.append(f"Missing required certs: {', '.join(missing_certs)}")
        if matched_skills:
            rationale_parts.append(f"Satisfies skills: {', '.join(matched_skills)}")
        if missing_skills:
            rationale_parts.append(f"Missing skills: {', '.join(missing_skills)}")

        if is_eligible:
            eligible_set.append(
                {
                    "employee_id": c_emp_id,
                    "name": c_name,
                    "eligible": True,
                    "matched_skills": matched_skills,
                    "missing_skills": missing_skills,
                    "matched_certs": matched_certs,
                    "missing_certs": missing_certs,
                    "rationale": "; ".join(rationale_parts) or "Meets all criteria.",
                }
            )

    return {
        "namespace_id": str(ns_uuid),
        "requirement_summary": {
            "required_skills": req_skills,
            "required_certs": req_certs,
        },
        "eligible_set": eligible_set,
        "eligible_count": len(eligible_set),
    }
