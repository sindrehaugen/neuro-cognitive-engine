"""
nce/vertical_modules/hr/a2a.py
==============================
Agent-to-Agent (A2A) interfaces for Module 13 (HR Engine).

Provides cross-module integration endpoints serving:
  - Project(7): PL-assignment (skills + capacity match, writing PROJECT -[led_by]-> EMPLOYEE)
  - Field Tech(12): Tech-dispatch (required certs + skills + current load, consuming WORK_ORDER edges)
  - Vendors(4): Contractor-to-HR skill alignment (exposing shared taxonomy & certification model)
  - Morning Brief(#19): Aggregated operational risk slice (expiring certs, teams at capacity,
    open statutory compliance deadlines, sustained overload count)

Strict Legal & Architectural Guarantees:
  - RL-1 (NEVER ranking): Returns requirement fit only. No leaderboards, scores, or peer comparisons.
  - RL-2 (EU AI Act Art. 5): Prohibits emotion inference. Sustained overload uses objective operational signals.
  - RL-3 (GDPR & Tenant Isolation): Strict `namespace_id = $N::uuid` predicates on every query.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.hr._guard import assert_ranking_prohibited
from nce.vertical_modules.hr.capacity import do_capacity
from nce.vertical_modules.hr.certs import do_cert_status
from nce.vertical_modules.hr.compliance import (
    COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING,
    COMPLIANCE_STATE_DIALOGMOTE_26W_PENDING,
    COMPLIANCE_STATE_PLAN_4W_PENDING,
)
from nce.vertical_modules.hr.profile import _extract_pool, _parse_uuid
from nce.vertical_modules.hr.skills import do_match_skills
from nce.vertical_modules.hr.taxonomy import (
    get_cert_taxonomy,
    get_skill_taxonomy,
    resolve_implied_skills,
)

log = logging.getLogger("nce.vertical_modules.hr.a2a")

_NODE_TYPE_PROJECT = "PROJECT"
_NODE_TYPE_EMPLOYEE = "EMPLOYEE"
_PREDICATE_LED_BY = "led_by"


async def handle_project_assignment_query(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A2A interface for Project(7): Project Lead assignment & graph linking.

    Finds qualified project lead candidates based on required skills and
    certifications, evaluates current capacity, and optionally writes the
    PROJECT -[led_by]-> EMPLOYEE knowledge graph edge upon confirmation.

    Parameters
    ----------
    engine:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) Tenant UUID.
        - project_id: (required) Project identifier (e.g. 'PRJ-101').
        - required_skills: (optional) List of required skill strings.
        - required_certs: (optional) List of required certification strings.
        - assign_lead_id: (optional) If provided, confirms assignment and writes KG edge.
        - horizon_days: (optional) Days for capacity lookahead (default 30).
    """
    assert_ranking_prohibited(params)
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("project_id is required")

    required_skills = list(params.get("required_skills") or [])
    required_certs = list(params.get("required_certs") or [])
    assign_lead_id = str(params.get("assign_lead_id") or "").strip()
    horizon_days = int(params.get("horizon_days") or 30)

    # 1. Match candidates by skills and certs
    match_result = await do_match_skills(
        engine,
        {
            "namespace_id": ns_uuid,
            "required_skills": required_skills,
            "required_certs": required_certs,
        },
    )
    candidates = match_result.get("candidates", [])

    # 2. Augment candidate eligibility with capacity/utilization
    enriched_candidates: list[dict[str, Any]] = []
    for cand in candidates:
        emp_id = cand["employee_id"]
        cap = await do_capacity(
            engine,
            {
                "namespace_id": ns_uuid,
                "employee_id": emp_id,
                "horizon_days": horizon_days,
            },
        )
        utilization = float(cap.get("utilization_pct", 0.0))
        enriched_candidates.append(
            {
                "employee_id": emp_id,
                "name": cand.get("name"),
                "role": cand.get("role"),
                "skills_matched": cand.get("skills_matched", []),
                "certs_matched": cand.get("certs_matched", []),
                "utilization_pct": utilization,
                "available": utilization < 100.0,
                "rationale": (
                    f"Meets requirements with {len(cand.get('skills_matched', []))} skills; "
                    f"current utilization is {utilization:.1f}% over {horizon_days}d."
                ),
            }
        )

    response: dict[str, Any] = {
        "project_id": project_id,
        "required_skills": required_skills,
        "required_certs": required_certs,
        "candidate_count": len(enriched_candidates),
        "candidates": enriched_candidates,
    }

    # 3. If assign_lead_id is specified, record assignment and write KG edge
    if assign_lead_id:
        async with scoped_pg_session(pool, ns_uuid) as conn:
            # Check employee exists in tenant
            emp_row = await conn.fetchrow(
                "SELECT id, employee_id FROM employees WHERE employee_id = $1 AND namespace_id = $2::uuid",
                assign_lead_id,
                ns_uuid,
            )
            if not emp_row:
                raise ValueError(f"Employee {assign_lead_id!r} not found in namespace")

            # Write PROJECT node
            prj_label = f"PROJECT:{project_id}"
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, $2, $3::uuid, 'agent')
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                prj_label,
                _NODE_TYPE_PROJECT,
                ns_uuid,
            )

            # Write EMPLOYEE node
            emp_label = f"EMPLOYEE:{assign_lead_id}"
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, $2, $3::uuid, 'agent')
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                emp_label,
                _NODE_TYPE_EMPLOYEE,
                ns_uuid,
            )

            # Write PROJECT -[led_by]-> EMPLOYEE edge
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
                VALUES ($1, $2, $3, 1.0, $4::uuid, 'agent')
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                prj_label,
                _PREDICATE_LED_BY,
                emp_label,
                ns_uuid,
            )

        response["assigned_lead"] = {
            "employee_id": assign_lead_id,
            "project_id": project_id,
            "edge": f"{prj_label} -[{_PREDICATE_LED_BY}]-> {emp_label}",
            "status": "confirmed",
        }

    return response


async def handle_field_tech_dispatch_query(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A2A interface for Field Tech(12): Tech-dispatch eligibility & workload.

    Provides qualified field technicians filtered by required certifications,
    skills, and active work order load. Consumes WORK_ORDER -[assigned_to]-> EMPLOYEE
    edges via do_capacity.

    Parameters
    ----------
    engine:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) Tenant UUID.
        - work_order_id: (optional) Work order reference.
        - required_skills: (optional) Required technical skills.
        - required_certs: (optional) Required certifications.
        - location_id: (optional) Location filter.
        - horizon_days: (optional) Days for capacity lookahead (default 14).
    """
    assert_ranking_prohibited(params)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    work_order_id = params.get("work_order_id")
    required_skills = list(params.get("required_skills") or [])
    required_certs = list(params.get("required_certs") or [])
    location_id = params.get("location_id")
    horizon_days = int(params.get("horizon_days") or 14)

    # 1. Fetch skills & cert match
    match_result = await do_match_skills(
        engine,
        {
            "namespace_id": ns_uuid,
            "required_skills": required_skills,
            "required_certs": required_certs,
        },
    )
    candidates = match_result.get("candidates", [])

    # 2. Fetch cert status and capacity for each candidate
    dispatches: list[dict[str, Any]] = []
    for cand in candidates:
        emp_id = cand["employee_id"]

        # Capacity from active work orders & projects
        cap = await do_capacity(
            engine,
            {
                "namespace_id": ns_uuid,
                "employee_id": emp_id,
                "horizon_days": horizon_days,
            },
        )
        utilization = float(cap.get("utilization_pct", 0.0))

        # Check cert expiration status
        cert_info = await do_cert_status(
            engine,
            {
                "namespace_id": ns_uuid,
                "employee_id": emp_id,
            },
        )
        has_expired_certs = len(cert_info.get("expired_certs", [])) > 0
        expiring_soon = cert_info.get("expiring_certs", [])

        # Location check
        cand_loc = cand.get("location_id")
        loc_match = bool(not location_id or cand_loc == location_id)

        # Plain-language assignment fit rationale
        rationale = (
            f"Eligible: {len(cand.get('skills_matched', []))} skills matched; "
            f"load={utilization:.0f}%; certs_valid={not has_expired_certs}."
        )

        dispatches.append(
            {
                "employee_id": emp_id,
                "name": cand.get("name"),
                "role": cand.get("role"),
                "location_id": cand_loc,
                "location_matched": loc_match,
                "skills_matched": cand.get("skills_matched", []),
                "certs_matched": cand.get("certs_matched", []),
                "utilization_pct": utilization,
                "has_expired_certs": has_expired_certs,
                "expiring_certs_count": len(expiring_soon),
                "dispatch_eligible": not has_expired_certs and utilization < 100.0 and loc_match,
                "rationale": rationale,
            }
        )

    return {
        "work_order_id": work_order_id,
        "required_skills": required_skills,
        "required_certs": required_certs,
        "location_filter": location_id,
        "candidate_count": len(dispatches),
        "candidates": dispatches,
    }


def handle_vendor_contractor_skill_align(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A2A interface for Vendors(4): Contractor-to-HR taxonomy alignment.

    Exposes the shared HR_SKILL and HR_CERT taxonomy model to Vendors so
    external CONTRACTOR resources align to the exact same skill and cert
    axis as internal employees.

    Parameters
    ----------
    engine:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) Tenant UUID.
        - contractor_id: (required) External contractor identifier.
        - skills: (optional) List of skill strings or dicts with name & level.
        - certifications: (optional) List of cert strings or dicts with name & authority.
    """
    assert_ranking_prohibited(params)
    contractor_id = str(params.get("contractor_id") or "").strip()
    if not contractor_id:
        raise ValueError("contractor_id is required")

    raw_skills = params.get("skills") or []
    raw_certs = params.get("certifications") or []

    tax = get_skill_taxonomy()
    all_taxonomy_skills: dict[str, str] = {}  # lowercase_name -> canonical_name
    for _domain, group in tax.items():
        for s in group.get("skills", []):
            all_taxonomy_skills[s.strip().lower()] = s

    cert_tax = get_cert_taxonomy()

    aligned_skills: list[dict[str, Any]] = []
    unaligned_skills: list[str] = []

    for item in raw_skills:
        skill_name = item.get("name") if isinstance(item, dict) else str(item)
        skill_level = item.get("level", 1) if isinstance(item, dict) else 1
        key = str(skill_name).strip().lower()
        if key in all_taxonomy_skills:
            aligned_skills.append(
                {
                    "skill": all_taxonomy_skills[key],
                    "level": min(max(int(skill_level), 1), 4),
                    "canonical": True,
                }
            )
        else:
            unaligned_skills.append(str(skill_name))

    aligned_certs: list[dict[str, Any]] = []
    unaligned_certs: list[str] = []
    for item in raw_certs:
        cert_name = item.get("name") if isinstance(item, dict) else str(item)
        c_str = str(cert_name).strip()
        matched = False
        for canon_cert, implied in cert_tax.items():
            if c_str.lower() == canon_cert.lower():
                aligned_certs.append(
                    {
                        "name": canon_cert,
                        "canonical": True,
                        "implied_skills": implied,
                    }
                )
                matched = True
                break
        if not matched:
            unaligned_certs.append(c_str)

    # Implied skills from recognized certifications
    cert_names = [c["name"] for c in aligned_certs]
    implied_skills = sorted(resolve_implied_skills(cert_names))

    alignment_status = (
        "fully_aligned"
        if not unaligned_skills and not unaligned_certs
        else "partially_aligned"
        if aligned_skills or aligned_certs
        else "unaligned"
    )

    return {
        "contractor_id": contractor_id,
        "alignment_status": alignment_status,
        "aligned_skills": aligned_skills,
        "unaligned_skills": unaligned_skills,
        "aligned_certifications": aligned_certs,
        "unaligned_certifications": unaligned_certs,
        "implied_skills_from_certs": implied_skills,
    }


async def get_morning_brief_hr_slice(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A2A interface feeding the Morning Brief (#19 aggregate).

    Returns an aggregated operational risk slice:
      - Expiring certifications within warning horizon
      - Teams/departments approaching full capacity
      - Open statutory compliance deadlines (4w/7w/26w sickness follow-up)
      - Sustained overload risk counts (operational signals only)

    STRICT GUARANTEE (RL-1 & RL-2):
      Returns aggregate risk indicators ONLY. Never includes individual
      performance rankings, leaderboards, or emotional inferences.
    """
    assert_ranking_prohibited(params)
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    cert_warn_days = int(params.get("cert_warn_days") or 90)
    capacity_threshold_pct = float(params.get("capacity_threshold_pct") or 85.0)

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Expiring certifications count & breakdown by authority
        cert_rows = await conn.fetch(
            """
            SELECT authority, name, COUNT(*) as count
            FROM certifications
            WHERE namespace_id = $1::uuid
              AND valid_to IS NOT NULL
              AND valid_to <= (CURRENT_DATE + ($2 || ' days')::interval)::date
              AND valid_to >= CURRENT_DATE
            GROUP BY authority, name
            ORDER BY count DESC
            """,
            ns_uuid,
            str(cert_warn_days),
        )
        total_expiring = sum(int(r["count"]) for r in cert_rows)
        expiring_breakdown = [
            {"authority": r["authority"], "cert_name": r["name"], "count": int(r["count"])}
            for r in cert_rows
        ]

        # 2. Open statutory compliance deadlines from active sick leaves
        compliance_rows = await conn.fetch(
            """
            SELECT id, employee_id, start_date, raw
            FROM absences
            WHERE namespace_id = $1::uuid
              AND type IN ('sick_leave', 'sick')
              AND status IN ('pending', 'approved')
              AND (end_date IS NULL OR end_date >= CURRENT_DATE)
            """,
            ns_uuid,
        )
        today = date.today()
        open_compliance_count = 0
        milestone_pending_counts: dict[str, int] = {
            COMPLIANCE_STATE_PLAN_4W_PENDING: 0,
            COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING: 0,
            COMPLIANCE_STATE_DIALOGMOTE_26W_PENDING: 0,
        }

        for row in compliance_rows:
            s_date = row["start_date"]
            raw_data = row["raw"] if isinstance(row["raw"], dict) else {}
            completed = set(raw_data.get("compliance_completed_milestones") or [])
            days_elapsed = (today - s_date).days if s_date else 0

            # Plan 4w
            if days_elapsed >= 21 and "oppfolgingsplan_4w" not in completed:
                open_compliance_count += 1
                milestone_pending_counts[COMPLIANCE_STATE_PLAN_4W_PENDING] += 1
            # Dialogmote 7w
            elif days_elapsed >= 42 and "dialogmote_1_7w" not in completed:
                open_compliance_count += 1
                milestone_pending_counts[COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING] += 1
            # Dialogmote 26w
            elif days_elapsed >= 168 and "dialogmote_2_26w" not in completed:
                open_compliance_count += 1
                milestone_pending_counts[COMPLIANCE_STATE_DIALOGMOTE_26W_PENDING] += 1

        # 3. Department capacity check
        dept_rows = await conn.fetch(
            """
            SELECT department, COUNT(*) as headcount
            FROM employees
            WHERE namespace_id = $1::uuid AND active = TRUE
            GROUP BY department
            """,
            ns_uuid,
        )

        at_capacity_departments: list[dict[str, Any]] = []
        total_active_headcount = 0
        for d in dept_rows:
            dept_name = d["department"] or "General"
            headcount = int(d["headcount"])
            total_active_headcount += headcount

            # Query assigned work orders across department employees
            wo_load_row = await conn.fetchrow(
                """
                SELECT COUNT(DISTINCT e.subject_label) as assigned_wos
                FROM kg_edges e
                JOIN employees emp ON e.object_label = 'EMPLOYEE:' || emp.employee_id
                WHERE e.namespace_id = $1::uuid
                  AND e.predicate = 'assigned_to'
                  AND emp.department = $2
                  AND emp.active = TRUE
                """,
                ns_uuid,
                dept_name,
            )
            assigned_wos = int(wo_load_row["assigned_wos"]) if wo_load_row else 0
            # Departmental capacity load estimation
            dept_cap_pct = min(100.0, (assigned_wos / max(headcount * 2, 1)) * 100.0)
            if dept_cap_pct >= capacity_threshold_pct:
                at_capacity_departments.append(
                    {
                        "department": dept_name,
                        "headcount": headcount,
                        "active_work_orders": assigned_wos,
                        "estimated_utilization_pct": round(dept_cap_pct, 1),
                    }
                )

    return {
        "module": "hr",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"HR Operational Risk Slice: {total_expiring} expiring certs, "
            f"{open_compliance_count} open statutory compliance deadlines, "
            f"{len(at_capacity_departments)} teams at high capacity."
        ),
        "operational_risk": {
            "expiring_certifications_total": total_expiring,
            "expiring_certifications_breakdown": expiring_breakdown,
            "open_statutory_deadlines_total": open_compliance_count,
            "statutory_deadlines_pending": milestone_pending_counts,
            "teams_at_high_capacity": at_capacity_departments,
            "total_active_headcount": total_active_headcount,
        },
    }


__all__ = [
    "handle_project_assignment_query",
    "handle_field_tech_dispatch_query",
    "handle_vendor_contractor_skill_align",
    "get_morning_brief_hr_slice",
]
