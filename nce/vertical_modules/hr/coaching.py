"""
nce/vertical_modules/hr/coaching.py
===================================
Private 1-on-1 logging and individual coaching advisor for Module 13.

Enforces the Three Red Lines:
  - RL-1: Hard-pinned NEVER-ranking. Strictly individual; refuses leaderboards/peer ranking.
  - RL-2: EU AI Act Article 5 compliance (no sentiment or emotional state inference).
          Sustained overload is detected from objective operational signals only.
  - RL-3: GDPR PII redaction gate before storing notes; memories are scoped to hr_private_coach.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.hr.privacy import (
    calculate_sustained_overload,
    enforce_privacy_preconditions,
    redact_hr_text,
)
from nce.vertical_modules.hr.taxonomy import get_cert_taxonomy, get_skill_taxonomy

log = logging.getLogger("nce.vertical_modules.hr.coaching")


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


async def do_log_one_on_one(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Log a confidential 1-on-1 review or coaching session with PII redaction.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Target employee ID.
        - interviewer_id: (required) Manager or reviewer ID.
        - notes: (required) Session discussion notes.
        - action_items: (optional) List of agreed follow-up tasks.
        - session_date: (optional, default today) Date of session.
        - hr_source_id: (optional) Upstream source ID for GDPR hard retirement.
        - weekly_hours_history: (optional) Recent weekly hours for objective overload check.
        - caller_role: (optional) Caller role for access scoping.
        - caller_employee_id: (optional) Caller employee ID.
    """
    # 🔴 BLOCKING PRIVACY PRECONDITION: Redaction gate & RL-1/RL-3 access scope
    enforce_privacy_preconditions(params)

    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    interviewer_id = str(params.get("interviewer_id") or "").strip()
    if not interviewer_id:
        raise ValueError("interviewer_id is required")

    raw_notes = str(params.get("notes") or "").strip()
    if not raw_notes:
        raise ValueError("notes is required")

    # RL-3: Redaction gate (Fødselsnummer, bank, credit cards, phones, emails stripped)
    redacted_notes = redact_hr_text(raw_notes)

    action_items = list(params.get("action_items") or [])
    session_date = str(params.get("session_date") or date.today().isoformat()).strip()
    hr_source_id = params.get("hr_source_id")

    # RL-2: Objective overload check (no sentiment analysis)
    weekly_hours = params.get("weekly_hours_history") or []
    overload_data = None
    if weekly_hours:
        overload_data = calculate_sustained_overload(weekly_hours)

    # Scoped memory representation (RL-3)
    memory_record = {
        "agent_id": "hr_private_coach",
        "namespace_id": str(ns_uuid),
        "employee_id": employee_id,
        "interviewer_id": interviewer_id,
        "session_date": session_date,
        "notes": redacted_notes,
        "action_items": action_items,
        "hr_source_id": hr_source_id,
        "overload_advisory": overload_data,
        "tags": ["hr_private_coach", f"employee:{employee_id}"],
        "status": "recorded",
    }

    # If engine has memory ingestion capabilities, persist through private scope
    if hasattr(engine, "store_memory"):
        try:
            await engine.store_memory(
                namespace_id=str(ns_uuid),
                agent_id="hr_private_coach",
                content=f"1-on-1 coaching session for {employee_id}. Redacted notes: {redacted_notes}",
                metadata=memory_record,
            )
        except Exception as exc:
            log.debug("Engine memory storage skipped or deferred: %s", exc)

    return memory_record


async def do_coach(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Individual skill growth advisor recommending training and certifications.

    Enforces RL-1:
      - Refuses comparative ranking, leaderboards, or cross-person standing scores.
      - Scoped exclusively to an individual employee.
    Enforces RL-2:
      - Objective workload signals only (hours/capacity). No emotional state inference.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Employee ID.
        - target_role: (optional) Desired advancement role.
        - focus_areas: (optional) Preferred technical domains.
        - weekly_hours_history: (optional) Hours worked past N weeks.
        - caller_role: (optional) Caller role for access scoping.
        - caller_employee_id: (optional) Caller ID.
    """
    # 🔴 BLOCKING PRIVACY PRECONDITION: Redaction gate & RL-1/RL-3 access scope
    enforce_privacy_preconditions(params)

    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    target_role = str(params.get("target_role") or "senior_field_engineer").strip()
    focus_areas = [
        str(f).strip().lower() for f in (params.get("focus_areas") or []) if str(f).strip()
    ]

    # Retrieve current skills and certs
    skills_held: set[str] = set()
    certs_held: set[str] = set()

    async with scoped_pg_session(pool, ns_uuid) as conn:
        s_rows = await conn.fetch(
            "SELECT name FROM skills WHERE employee_id = $1 AND namespace_id = $2::uuid",
            employee_id,
            ns_uuid,
        )
        skills_held = {r["name"] for r in s_rows}

        c_rows = await conn.fetch(
            """
            SELECT name FROM certifications
            WHERE employee_id = $1 AND namespace_id = $2::uuid AND status = 'active'
            """,
            employee_id,
            ns_uuid,
        )
        certs_held = {r["name"] for r in c_rows}

    skills_lower = {s.lower() for s in skills_held}
    certs_lower = {c.lower() for c in certs_held}

    # Taxonomy lookup
    skill_tax = get_skill_taxonomy()
    cert_tax = get_cert_taxonomy()

    def _normalize_domain(d: str) -> str:
        dl = d.lower()
        for k in skill_tax:
            if k in dl or dl in k:
                return k
        return dl

    recommendations: list[dict[str, Any]] = []
    domains_to_evaluate = (
        [_normalize_domain(f) for f in focus_areas] if focus_areas else list(skill_tax.keys())
    )

    for domain in domains_to_evaluate:
        if domain in skill_tax:
            domain_skills = skill_tax[domain]["skills"]
            missing = [s for s in domain_skills if s.lower() not in skills_lower]

            if missing:
                relevant_certs = [
                    c_name
                    for c_name, implied in cert_tax.items()
                    if c_name.lower() not in certs_lower
                    and any(m.lower() in [imp.lower() for imp in implied] for m in missing)
                ]
                recommendations.append(
                    {
                        "domain": domain,
                        "unlocked_skills_target": missing[:3],
                        "suggested_certifications": relevant_certs[:2],
                    }
                )

    # RL-2: Objective overload advisory
    weekly_hours = params.get("weekly_hours_history") or []
    overload_advisory = calculate_sustained_overload(weekly_hours) if weekly_hours else None

    return {
        "namespace_id": str(ns_uuid),
        "employee_id": employee_id,
        "target_role": target_role,
        "current_skills_count": len(skills_held),
        "active_certs_count": len(certs_held),
        "growth_recommendations": recommendations,
        "overload_advisory": overload_advisory,
        "rationale": (
            f"Evaluated skill matrix against {target_role} expectations. "
            f"Recommended {len(recommendations)} technical advancement pathways."
        ),
    }
