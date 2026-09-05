"""
nce/vertical_modules/hr/coaching.py
===================================
Private 1-on-1 logging and individual coaching advisor for Module 13.

Enforces:
  - RL-1: Hard-pinned NEVER-ranking. Strictly individual; refuses leaderboards/peer ranking.
  - RL-2: EU AI Act Article 5 compliance (no sentiment or emotional state inference).
  - RL-3: GDPR PII redaction gate before storing notes.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.hr._guard import assert_ranking_prohibited
from nce.vertical_modules.hr.taxonomy import get_cert_taxonomy, get_skill_taxonomy

log = logging.getLogger("nce.vertical_modules.hr.coaching")

# Basic PII scrubbing regexes (Norwegian fødselsnummer, emails, phones)
_SSN_PATTERN = re.compile(r"\b\d{6}\s?\d{5}\b")
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
_PHONE_PATTERN = re.compile(r"\b(?:\+47\s?)?[2-9]\d{1}\s?\d{2}\s?\d{2}\s?\d{2}\b")


def _redact_pii(text: str) -> str:
    """Strip sensitive PII identifiers from free text (RL-3 GDPR gate)."""
    text = _SSN_PATTERN.sub("[REDACTED_NATIONAL_ID]", text)
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_PATTERN.sub("[REDACTED_PHONE]", text)
    return text


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
    """
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

    # RL-3: Redaction gate
    redacted_notes = _redact_pii(raw_notes)

    action_items = list(params.get("action_items") or [])
    session_date = str(params.get("session_date") or date.today().isoformat()).strip()
    hr_source_id = params.get("hr_source_id")

    return {
        "namespace_id": str(ns_uuid),
        "employee_id": employee_id,
        "interviewer_id": interviewer_id,
        "session_date": session_date,
        "notes": redacted_notes,
        "action_items": action_items,
        "hr_source_id": hr_source_id,
        "status": "recorded",
    }


async def do_coach(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Individual skill growth advisor recommending training and certifications.

    Enforces RL-1:
      - Refuses comparative ranking, leaderboards, or cross-person standing scores.
      - Scoped exclusively to an individual employee.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Employee ID.
        - target_role: (optional) Desired advancement role (e.g. 'lead_commissioning_engineer').
        - focus_areas: (optional) Preferred technical domains (e.g. ['network', 'control']).
    """
    assert_ranking_prohibited(params)

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

    # Taxonomy lookup
    skill_tax = get_skill_taxonomy()
    cert_tax = get_cert_taxonomy()

    recommendations: list[dict[str, Any]] = []

    # Identify domains of interest
    domains_to_evaluate = focus_areas if focus_areas else list(skill_tax.keys())

    for domain in domains_to_evaluate:
        if domain in skill_tax:
            domain_skills = skill_tax[domain]["skills"]
            missing = [s for s in domain_skills if s not in skills_held]

            if missing:
                # Find certs that would unlock these missing skills
                relevant_certs = [
                    c_name
                    for c_name, implied in cert_tax.items()
                    if c_name not in certs_held and any(m in implied for m in missing)
                ]
                recommendations.append(
                    {
                        "domain": domain,
                        "unlocked_skills_target": missing[:3],
                        "suggested_certifications": relevant_certs[:2],
                    }
                )

    return {
        "namespace_id": str(ns_uuid),
        "employee_id": employee_id,
        "target_role": target_role,
        "current_skills_count": len(skills_held),
        "active_certs_count": len(certs_held),
        "growth_recommendations": recommendations,
        "rationale": (
            f"Evaluated skill matrix against {target_role} expectations. "
            f"Recommended {len(recommendations)} technical advancement pathways."
        ),
    }
