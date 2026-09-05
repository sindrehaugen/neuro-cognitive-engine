"""
nce/vertical_modules/hr/compliance.py
=====================================
Norwegian statutory compliance state machine and sickness follow-up tracking
for Module 13 (HR Engine).

Complies with:
  - Norwegian Working Environment Act (Arbeidsmiljoloven) & Folketrygdloven
  - Arbeidstilsynet / NAV / NHO statutory sick-leave follow-up timeline:
      * Oppfolgingsplan (Follow-up plan): within 4 weeks (28 days)
      * Dialogmote 1 (Employer-convened meeting): within 7 weeks (49 days)
      * Dialogmote 2 (NAV-convened meeting): within 26 weeks (182 days)
  - Early warning thresholds:
      * Day 21: Alert for 4-week Oppfolgingsplan (7 days prior)
      * Day 42: Alert for 7-week Dialogmote 1 (7 days prior)
      * Day 168: Alert for 26-week Dialogmote 2 (14 days prior)
  - Confidentiality & privacy:
      * Scoped strictly to Manager / HR / Verneombud roles
      * Objective operational timeline only -- NO sentiment inference (RL-2)
      * NEVER ranking or comparative scoring (RL-1)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.hr.compliance")

EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED: str = "hr_compliance_milestone_recorded"

STATUTORY_OPPFOLGINGSPLAN_DAYS = 28  # 4 weeks
STATUTORY_DIALOGMOTE_1_DAYS = 49  # 7 weeks
STATUTORY_DIALOGMOTE_2_DAYS = 182  # 26 weeks (~6 months)

WARN_OPPFOLGINGSPLAN_DAYS = 21  # 3 weeks
WARN_DIALOGMOTE_1_DAYS = 42  # 6 weeks
WARN_DIALOGMOTE_2_DAYS = 168  # 24 weeks

COMPLIANCE_STATE_NORMAL = "normal"
COMPLIANCE_STATE_PLAN_4W_PENDING = "plan_4w_pending"
COMPLIANCE_STATE_PLAN_4W_COMPLETED = "plan_4w_completed"
COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING = "dialogmote_7w_pending"
COMPLIANCE_STATE_DIALOGMOTE_7W_COMPLETED = "dialogmote_7w_completed"
COMPLIANCE_STATE_DIALOGMOTE_26W_PENDING = "dialogmote_26w_pending"
COMPLIANCE_STATE_DIALOGMOTE_26W_COMPLETED = "dialogmote_26w_completed"
COMPLIANCE_STATE_EXEMPT = "exempt"


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


def _parse_date(val: Any, name: str) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val).strip())
    except Exception as exc:
        raise ValueError(f"Invalid ISO date for {name}: {val!r}") from exc


def evaluate_absence_compliance(
    absence_type: str,
    start_date: date,
    as_of_date: date | None = None,
    existing_compliance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure evaluation of Norwegian statutory sick leave follow-up milestones.

    Parameters
    ----------
    absence_type : str
        Type of absence (only 'sick' / 'sick_leave' triggers statutory timeline).
    start_date : date
        Start date of the sickness period.
    as_of_date : date, optional
        Reference evaluation date (default: today).
    existing_compliance : dict[str, Any], optional
        Previously recorded compliance state and completed milestones.
    """
    if as_of_date is None:
        as_of_date = date.today()

    norm_type = (absence_type or "").strip().lower()
    is_sick = norm_type in ("sick", "sick_leave")

    comp = dict(existing_compliance or {})
    milestones = comp.get("milestones") or {}

    if not is_sick:
        return {
            "applicable": False,
            "compliance_state": COMPLIANCE_STATE_NORMAL,
            "days_elapsed": max(0, (as_of_date - start_date).days),
            "alerts": [],
            "milestones": {},
            "verneombud_alert": False,
        }

    days_elapsed = max(0, (as_of_date - start_date).days)
    alerts: list[dict[str, Any]] = []

    # Milestone 1: 4-week Oppfølgingsplan
    plan_deadline = start_date + timedelta(days=STATUTORY_OPPFOLGINGSPLAN_DAYS)
    plan_done = milestones.get("plan_4w", {}).get("completed", False)
    plan_status = (
        "completed"
        if plan_done
        else (
            "overdue"
            if days_elapsed > STATUTORY_OPPFOLGINGSPLAN_DAYS
            else ("pending_warning" if days_elapsed >= WARN_OPPFOLGINGSPLAN_DAYS else "pending")
        )
    )
    if plan_status == "overdue":
        alerts.append(
            {
                "code": "PLAN_4W_OVERDUE",
                "severity": "critical",
                "milestone": "plan_4w",
                "deadline": plan_deadline.isoformat(),
                "message": f"Statutory 4-week Oppfolgingsplan is overdue by {days_elapsed - STATUTORY_OPPFOLGINGSPLAN_DAYS} days.",
            }
        )
    elif plan_status == "pending_warning":
        alerts.append(
            {
                "code": "PLAN_4W_UPCOMING",
                "severity": "warning",
                "milestone": "plan_4w",
                "deadline": plan_deadline.isoformat(),
                "message": f"Statutory 4-week Oppfolgingsplan due in {STATUTORY_OPPFOLGINGSPLAN_DAYS - days_elapsed} days.",
            }
        )

    # Milestone 2: 7-week Dialogmøte 1
    dm1_deadline = start_date + timedelta(days=STATUTORY_DIALOGMOTE_1_DAYS)
    dm1_done = milestones.get("dialogmote_1", {}).get("completed", False)
    dm1_status = (
        "completed"
        if dm1_done
        else (
            "overdue"
            if days_elapsed > STATUTORY_DIALOGMOTE_1_DAYS
            else ("pending_warning" if days_elapsed >= WARN_DIALOGMOTE_1_DAYS else "pending")
        )
    )
    if dm1_status == "overdue":
        alerts.append(
            {
                "code": "DIALOGMOTE_1_OVERDUE",
                "severity": "critical",
                "milestone": "dialogmote_1",
                "deadline": dm1_deadline.isoformat(),
                "message": f"Statutory 7-week Dialogmote 1 is overdue by {days_elapsed - STATUTORY_DIALOGMOTE_1_DAYS} days.",
            }
        )
    elif dm1_status == "pending_warning":
        alerts.append(
            {
                "code": "DIALOGMOTE_1_UPCOMING",
                "severity": "warning",
                "milestone": "dialogmote_1",
                "deadline": dm1_deadline.isoformat(),
                "message": f"Statutory 7-week Dialogmote 1 due in {STATUTORY_DIALOGMOTE_1_DAYS - days_elapsed} days.",
            }
        )

    # Milestone 3: 26-week Dialogmøte 2 (NAV)
    dm2_deadline = start_date + timedelta(days=STATUTORY_DIALOGMOTE_2_DAYS)
    dm2_done = milestones.get("dialogmote_2", {}).get("completed", False)
    dm2_status = (
        "completed"
        if dm2_done
        else (
            "overdue"
            if days_elapsed > STATUTORY_DIALOGMOTE_2_DAYS
            else ("pending_warning" if days_elapsed >= WARN_DIALOGMOTE_2_DAYS else "pending")
        )
    )
    if dm2_status == "overdue":
        alerts.append(
            {
                "code": "DIALOGMOTE_2_OVERDUE",
                "severity": "critical",
                "milestone": "dialogmote_2",
                "deadline": dm2_deadline.isoformat(),
                "message": f"Statutory 26-week Dialogmote 2 (NAV) is overdue by {days_elapsed - STATUTORY_DIALOGMOTE_2_DAYS} days.",
            }
        )
    elif dm2_status == "pending_warning":
        alerts.append(
            {
                "code": "DIALOGMOTE_2_UPCOMING",
                "severity": "warning",
                "milestone": "dialogmote_2",
                "deadline": dm2_deadline.isoformat(),
                "message": f"Statutory 26-week Dialogmote 2 (NAV) due in {STATUTORY_DIALOGMOTE_2_DAYS - days_elapsed} days.",
            }
        )

    # Overall compliance state resolution
    if days_elapsed >= STATUTORY_DIALOGMOTE_2_DAYS:
        compliance_state = (
            COMPLIANCE_STATE_DIALOGMOTE_26W_COMPLETED
            if dm2_done
            else COMPLIANCE_STATE_DIALOGMOTE_26W_PENDING
        )
    elif days_elapsed >= STATUTORY_DIALOGMOTE_1_DAYS:
        compliance_state = (
            COMPLIANCE_STATE_DIALOGMOTE_7W_COMPLETED
            if dm1_done
            else COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING
        )
    elif days_elapsed >= STATUTORY_OPPFOLGINGSPLAN_DAYS:
        compliance_state = (
            COMPLIANCE_STATE_PLAN_4W_COMPLETED if plan_done else COMPLIANCE_STATE_PLAN_4W_PENDING
        )
    else:
        compliance_state = COMPLIANCE_STATE_NORMAL

    # Verneombud / Safety representative involvement alert
    verneombud_alert = days_elapsed >= WARN_DIALOGMOTE_1_DAYS or any(
        a["severity"] == "critical" for a in alerts
    )

    return {
        "applicable": True,
        "compliance_state": compliance_state,
        "days_elapsed": days_elapsed,
        "start_date": start_date.isoformat(),
        "as_of_date": as_of_date.isoformat(),
        "alerts": alerts,
        "verneombud_alert": verneombud_alert,
        "milestones": {
            "plan_4w": {
                "deadline": plan_deadline.isoformat(),
                "status": plan_status,
                "completed": plan_done,
                "completed_at": milestones.get("plan_4w", {}).get("completed_at"),
                "authority": "Arbeidsgiver & Ansatt (deles med fastlege/sykmelder)",
            },
            "dialogmote_1": {
                "deadline": dm1_deadline.isoformat(),
                "status": dm1_status,
                "completed": dm1_done,
                "completed_at": milestones.get("dialogmote_1", {}).get("completed_at"),
                "authority": "Arbeidsgiver innkaller",
            },
            "dialogmote_2": {
                "deadline": dm2_deadline.isoformat(),
                "status": dm2_status,
                "completed": dm2_done,
                "completed_at": milestones.get("dialogmote_2", {}).get("completed_at"),
                "authority": "NAV innkaller",
            },
        },
    }


async def do_update_absence_compliance(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record completion or advancement of a Norwegian compliance milestone.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - absence_id: (required) Absence record ID.
        - milestone: (required) 'plan_4w', 'dialogmote_1', or 'dialogmote_2'.
        - completed: (optional, default True) Completion flag.
        - completed_at: (optional) ISO date string.
        - participants: (optional) List of participating roles/names.
        - notes: (optional) Compliance documentation notes.
        - nav_notified: (optional, default False) Whether NAV submission occurred.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    absence_id = str(params.get("absence_id") or "").strip()
    if not absence_id:
        raise ValueError("absence_id is required")

    milestone = str(params.get("milestone") or "").strip().lower()
    valid_milestones = {"plan_4w", "dialogmote_1", "dialogmote_2"}
    if milestone not in valid_milestones:
        raise ValueError(f"milestone must be one of {sorted(valid_milestones)}, got {milestone!r}")

    completed = bool(params.get("completed", True))
    completed_at_str = params.get("completed_at")
    completed_at = (
        _parse_date(completed_at_str, "completed_at") if completed_at_str else date.today()
    )

    participants = list(params.get("participants") or [])
    notes = str(params.get("notes") or "").strip()
    nav_notified = bool(params.get("nav_notified", False))

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, absence_id, employee_id, namespace_id, type,
                   start_date, end_date, days, status, compliance_state, raw
            FROM absences
            WHERE absence_id = $1 AND namespace_id = $2::uuid
            """,
            absence_id,
            ns_uuid,
        )
        if not row:
            raise ValueError(f"Absence record not found: {absence_id}")

        start_d = row["start_date"]
        if isinstance(start_d, datetime):
            start_d = start_d.date()

        raw = json.loads(row["raw"]) if isinstance(row["raw"], str) else dict(row["raw"] or {})
        comp = raw.get("compliance") or {}
        milestones = comp.get("milestones") or {}

        milestones[milestone] = {
            "completed": completed,
            "completed_at": completed_at.isoformat(),
            "participants": participants,
            "notes": notes,
            "nav_notified": nav_notified,
        }
        comp["milestones"] = milestones

        # Re-evaluate compliance state
        eval_res = evaluate_absence_compliance(
            absence_type=row["type"],
            start_date=start_d,
            as_of_date=date.today(),
            existing_compliance=comp,
        )

        comp["last_evaluation"] = eval_res
        new_state = eval_res["compliance_state"]
        raw["compliance"] = comp

        await conn.execute(
            """
            UPDATE absences
            SET compliance_state = $1,
                raw = $2::jsonb,
                updated_at = now()
            WHERE absence_id = $3 AND namespace_id = $4::uuid
            """,
            new_state,
            json.dumps(raw),
            absence_id,
            ns_uuid,
        )

    return {
        "absence_id": absence_id,
        "employee_id": row["employee_id"],
        "milestone": milestone,
        "completed": completed,
        "compliance_state": new_state,
        "evaluation": eval_res,
    }


async def do_query_compliance_deadlines(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Query active sick leave compliance deadlines across the namespace.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - only_alerts: (optional, default False) Return only records with pending/overdue alerts.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    only_alerts = bool(params.get("only_alerts", False))

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT a.absence_id, a.employee_id, e.name as employee_name, e.department,
                   a.type, a.start_date, a.end_date, a.days, a.status,
                   a.compliance_state, a.raw
            FROM absences a
            JOIN employees e ON e.employee_id = a.employee_id AND e.namespace_id = a.namespace_id
            WHERE a.namespace_id = $1::uuid
              AND a.type IN ('sick', 'sick_leave')
              AND a.status = 'approved'
              AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
            ORDER BY a.start_date ASC
            """,
            ns_uuid,
        )

    results: list[dict[str, Any]] = []
    total_alerts = 0

    for r in rows:
        st_d = r["start_date"]
        if isinstance(st_d, datetime):
            st_d = st_d.date()

        raw_data = json.loads(r["raw"]) if isinstance(r["raw"], str) else dict(r["raw"] or {})
        comp = raw_data.get("compliance") or {}

        evaluation = evaluate_absence_compliance(
            absence_type=r["type"],
            start_date=st_d,
            as_of_date=date.today(),
            existing_compliance=comp,
        )

        has_alerts = len(evaluation.get("alerts", [])) > 0
        if has_alerts:
            total_alerts += len(evaluation["alerts"])

        if only_alerts and not has_alerts:
            continue

        results.append(
            {
                "absence_id": r["absence_id"],
                "employee_id": r["employee_id"],
                "employee_name": r["employee_name"],
                "department": r["department"],
                "start_date": st_d.isoformat(),
                "days_elapsed": evaluation["days_elapsed"],
                "compliance_state": r["compliance_state"],
                "alerts": evaluation.get("alerts", []),
                "milestones": evaluation.get("milestones", {}),
                "verneombud_alert": evaluation.get("verneombud_alert", False),
            }
        )

    return {
        "namespace_id": str(ns_uuid),
        "total_active_sick_leaves": len(rows),
        "total_alerts": total_alerts,
        "records": results,
    }
