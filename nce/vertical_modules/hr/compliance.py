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

import asyncpg

from nce.db_utils import scoped_pg_session
from nce.events.bus import publish

log = logging.getLogger("nce.vertical_modules.hr.compliance")

EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED: str = "hr_compliance_milestone_recorded"
_NODE_TYPE_ABSENCE: str = "ABSENCE"
_OP_COMPLIANCE_ALERT: str = "compliance_alert"

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


async def do_check_compliance_deadlines(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Scan active sick leave absences in namespace and idempotently emit ABSENCE.compliance_alert.

    Periodically called by the HR compliance deadline watcher on cron (Wave HR-5).
    For each active sick-leave record, evaluates Norwegian statutory milestones
    (Oppfolgingsplan at 28d/21d, Dialogmote 1 at 49d/42d, Dialogmote 2 at 182d/168d),
    updates persistent compliance_state and raw JSONB in absences, and publishes
    ABSENCE.compliance_alert to the C4 transactional outbox under an atomic per-absence transaction.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) Tenant UUID string or UUID.
        - as_of_date: (optional) Reference evaluation date (defaults to current date).
        - absence_id: (optional) Restrict check to a single absence ID.

    Returns
    -------
    dict with:
        - namespace_id: str
        - checked: int
        - alerted: int (number of absences with new alerts emitted in this run)
        - published: int (total individual alert events emitted in this run)
        - total_active_sick_leaves: int
        - alerts: list[dict]
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    as_of_val = params.get("as_of_date") or params.get("now")
    if as_of_val is None:
        eval_date = date.today()
    elif isinstance(as_of_val, datetime):
        eval_date = as_of_val.date()
    elif isinstance(as_of_val, date):
        eval_date = as_of_val
    elif isinstance(as_of_val, str):
        try:
            eval_date = date.fromisoformat(as_of_val[:10])
        except Exception:
            eval_date = date.today()
    else:
        eval_date = date.today()

    target_absence_id = params.get("absence_id")
    if target_absence_id is not None:
        target_absence_id = str(target_absence_id).strip()

    checked_count = 0
    alerted_count = 0
    published_count = 0
    emitted_absence_alerts: list[dict[str, Any]] = []

    async with scoped_pg_session(pool, ns_uuid) as conn:
        if target_absence_id:
            rows = await conn.fetch(
                """
                SELECT a.id, a.absence_id, a.employee_id, a.type,
                       a.start_date, a.end_date, a.days, a.status,
                       a.compliance_state, a.raw
                FROM absences a
                WHERE a.namespace_id = $1::uuid
                  AND a.absence_id = $2
                """,
                ns_uuid,
                target_absence_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT a.id, a.absence_id, a.employee_id, a.type,
                       a.start_date, a.end_date, a.days, a.status,
                       a.compliance_state, a.raw
                FROM absences a
                WHERE a.namespace_id = $1::uuid
                  AND a.type IN ('sick', 'sick_leave')
                  AND a.status = 'approved'
                  AND (a.end_date IS NULL OR a.end_date >= $2::date)
                ORDER BY a.start_date ASC
                """,
                ns_uuid,
                eval_date,
            )

        checked_count = len(rows)

        for row in rows:
            abs_id = row["absence_id"]
            emp_id = row["employee_id"]
            st_d = row["start_date"]
            if isinstance(st_d, datetime):
                st_d = st_d.date()

            raw_data = (
                json.loads(row["raw"]) if isinstance(row["raw"], str) else dict(row["raw"] or {})
            )
            comp = raw_data.get("compliance") or {}
            emitted_alert_codes = list(comp.get("emitted_alert_codes") or [])

            evaluation = evaluate_absence_compliance(
                absence_type=row["type"],
                start_date=st_d,
                as_of_date=eval_date,
                existing_compliance=comp,
            )

            current_alerts = evaluation.get("alerts", [])
            new_alerts = [a for a in current_alerts if a.get("code") not in emitted_alert_codes]
            new_state = evaluation.get("compliance_state", COMPLIANCE_STATE_NORMAL)
            state_changed = new_state != row["compliance_state"]

            if new_alerts or state_changed:
                try:
                    async with conn.transaction():
                        # Update compliance structure
                        comp["last_evaluation"] = evaluation
                        if new_alerts:
                            all_emitted = sorted(
                                set(emitted_alert_codes)
                                | {a["code"] for a in new_alerts if a.get("code")}
                            )
                            comp["emitted_alert_codes"] = all_emitted
                        raw_data["compliance"] = comp

                        # 1. Update absences table
                        await conn.execute(
                            """
                            UPDATE absences
                            SET compliance_state = $1,
                                raw = $2::jsonb,
                                updated_at = now()
                            WHERE absence_id = $3 AND namespace_id = $4::uuid
                            """,
                            new_state,
                            json.dumps(raw_data),
                            abs_id,
                            ns_uuid,
                        )

                        # 2. Publish outbox event if there are new alerts
                        if new_alerts:
                            await publish(
                                conn,
                                namespace_id=ns_uuid,
                                node_type=_NODE_TYPE_ABSENCE,
                                op=_OP_COMPLIANCE_ALERT,
                                aggregate_id=f"ABSENCE:{abs_id}",
                                payload={
                                    "absence_id": abs_id,
                                    "employee_id": emp_id,
                                    "namespace_id": str(ns_uuid),
                                    "compliance_state": new_state,
                                    "alert_codes": [a["code"] for a in new_alerts],
                                    "alerts": new_alerts,
                                    "verneombud_alert": evaluation.get("verneombud_alert", False),
                                    "as_of_date": eval_date.isoformat(),
                                    "days_elapsed": evaluation.get("days_elapsed", 0),
                                },
                            )
                            alerted_count += 1
                            published_count += len(new_alerts)
                            emitted_absence_alerts.append(
                                {
                                    "absence_id": abs_id,
                                    "employee_id": emp_id,
                                    "compliance_state": new_state,
                                    "alerts": new_alerts,
                                    "verneombud_alert": evaluation.get("verneombud_alert", False),
                                }
                            )
                except Exception as exc:
                    log.exception(
                        "Failed to atomically persist and publish compliance alerts for absence=%s ns=%s: %s",
                        abs_id,
                        ns_uuid,
                        exc,
                    )
                    continue

    return {
        "namespace_id": str(ns_uuid),
        "checked": checked_count,
        "alerted": alerted_count,
        "published": published_count,
        "total_active_sick_leaves": checked_count,
        "alerts": emitted_absence_alerts,
    }


async def handle_absence_compliance_alert(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> None:
    """Acknowledge or process one ABSENCE.compliance_alert event from the outbox relay.

    Under the OutboxHandler contract:
    - Returning None signals successful delivery (no post-commit action needed).
    - Logs statutory alert details for auditability, telemetry, and monitoring.
    - Must not raise unless there is an unrecoverable configuration error.
    """
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    absence_id = payload.get("absence_id") or event.get("aggregate_id")
    employee_id = payload.get("employee_id")
    compliance_state = payload.get("compliance_state")
    alert_codes = payload.get("alert_codes", [])
    ns_id = event.get("namespace_id")

    log.info(
        "[hr.compliance] statutory compliance alert delivered: absence_id=%s employee_id=%s state=%s alerts=%s ns=%s",
        absence_id,
        employee_id,
        compliance_state,
        alert_codes,
        ns_id,
    )
    return None


def register_hr_compliance_subscribers() -> None:
    """Subscribe HR compliance outbox handlers to C4 transactional outbox bus.

    Subscribes to ABSENCE.compliance_alert.
    Idempotent: nce.events.bus.subscribe funnels through register_handler which
    ignores duplicate registrations by function equality.
    """
    try:
        from nce.events.bus import subscribe

        subscribe(
            {"node_type": _NODE_TYPE_ABSENCE, "op": _OP_COMPLIANCE_ALERT},
            handle_absence_compliance_alert,
        )
        log.info("HR compliance outbox subscriber registered for ABSENCE.compliance_alert.")
    except Exception as exc:
        log.warning("Could not register HR compliance outbox subscriber: %s", exc)
