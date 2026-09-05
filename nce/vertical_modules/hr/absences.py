"""
nce/vertical_modules/hr/absences.py
==================================
Absence, leave registration, and Norwegian statutory compliance integration
for Module 13 (HR Engine).

Functions:
  - do_register_absence: Register or update an absence record (Actor with confirmation).
  - do_query_absences: Query absence records with field-level privacy scoping.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.hr.compliance import evaluate_absence_compliance

log = logging.getLogger("nce.vertical_modules.hr.absences")

EVENT_TYPE_HR_ABSENCE_REGISTERED: str = "hr_absence_registered"

_VALID_ABSENCE_TYPES = frozenset(
    {"vacation", "sick_leave", "sick", "parental", "compassionate", "training", "other"}
)


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


def _parse_date(val: Any, field_name: str) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val).strip())
    except Exception as exc:
        raise ValueError(f"Invalid ISO date for {field_name}: {val!r}") from exc


async def do_register_absence(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Register or update an employee leave or absence event.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Employee ID.
        - absence_type: (required) vacation, sick_leave, parental, training, other.
        - start_date: (required) ISO date string.
        - end_date: (required) ISO date string.
        - absence_id: (optional) Unique absence ID. Generated if omitted.
        - days: (optional) Number of days. Computed from start/end if omitted.
        - reason: (optional) Free-text explanation. Sensitive PII.
        - status: (optional, default 'approved') approved, pending, rejected.
        - hr_source_id: (optional) Source ID for GDPR retirement.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    raw_type = str(params.get("absence_type") or params.get("type") or "").strip().lower()
    if raw_type not in _VALID_ABSENCE_TYPES:
        raise ValueError(
            f"absence_type must be one of {sorted(_VALID_ABSENCE_TYPES)}, got {raw_type!r}"
        )
    absence_type = "sick_leave" if raw_type == "sick" else raw_type

    start_d = _parse_date(params.get("start_date"), "start_date")
    end_d = _parse_date(params.get("end_date"), "end_date")
    if end_d < start_d:
        raise ValueError(f"end_date ({end_d}) cannot be before start_date ({start_d})")

    absence_id = str(params.get("absence_id") or f"ABS-{uuid4().hex[:8].upper()}").strip()
    days_val = params.get("days")
    if days_val is not None:
        days = float(days_val)
    else:
        days = float((end_d - start_d).days + 1)

    reason = params.get("reason")
    status = str(params.get("status") or "approved").strip().lower()
    hr_source_id = params.get("hr_source_id")

    raw = dict(params.get("raw") or {})

    # Evaluate Norwegian statutory compliance if sick leave
    comp_eval = evaluate_absence_compliance(
        absence_type=absence_type,
        start_date=start_d,
        as_of_date=date.today(),
        existing_compliance=raw.get("compliance"),
    )
    compliance_state = comp_eval["compliance_state"]
    raw["compliance"] = {
        "milestones": comp_eval.get("milestones", {}),
        "last_evaluation": comp_eval,
    }
    raw_json = json.dumps(raw)

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO absences (
                absence_id, employee_id, namespace_id, type,
                start_date, end_date, days, reason, status, compliance_state, hr_source_id, raw
            )
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
            ON CONFLICT (absence_id, namespace_id) DO UPDATE
            SET type = EXCLUDED.type,
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                days = EXCLUDED.days,
                reason = EXCLUDED.reason,
                status = EXCLUDED.status,
                compliance_state = EXCLUDED.compliance_state,
                hr_source_id = COALESCE(EXCLUDED.hr_source_id, absences.hr_source_id),
                raw = EXCLUDED.raw,
                updated_at = now()
            RETURNING id, absence_id, employee_id, namespace_id, type,
                      start_date, end_date, days, reason, status, compliance_state, hr_source_id, raw, created_at
            """,
            absence_id,
            employee_id,
            ns_uuid,
            absence_type,
            start_d,
            end_d,
            days,
            reason,
            status,
            compliance_state,
            hr_source_id,
            raw_json,
        )

    res_raw = json.loads(row["raw"]) if isinstance(row["raw"], str) else dict(row["raw"] or {})

    return {
        "id": str(row["id"]),
        "absence_id": row["absence_id"],
        "employee_id": row["employee_id"],
        "namespace_id": str(row["namespace_id"]),
        "absence_type": row["type"],
        "type": row["type"],
        "start_date": row["start_date"].isoformat() if row["start_date"] else None,
        "end_date": row["end_date"].isoformat() if row["end_date"] else None,
        "days": float(row["days"]),
        "status": row["status"],
        "compliance_state": row["compliance_state"],
        "compliance": res_raw.get("compliance"),
        "hr_source_id": row["hr_source_id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def do_query_absences(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Query employee absences with caller-role privacy scoping.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (optional) Filter by employee.
        - absence_type: (optional) Filter by absence type.
        - caller_role: (optional, default 'peer') Role of calling agent.
        - caller_employee_id: (optional) ID of caller if individual.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    caller_role = str(params.get("caller_role") or "peer").strip().lower()
    caller_emp_id = str(params.get("caller_employee_id") or "").strip()
    target_emp_id = params.get("employee_id")
    absence_type = params.get("absence_type") or params.get("type")

    # Only HR, manager, verneombud, or self can view sensitive reasons
    is_privileged = caller_role in ("hr", "manager", "verneombud", "admin", "system")

    conditions = ["namespace_id = $1::uuid"]
    query_args: list[Any] = [ns_uuid]
    idx = 2

    if target_emp_id:
        conditions.append(f"employee_id = ${idx}")
        query_args.append(str(target_emp_id).strip())
        idx += 1

    if absence_type:
        conditions.append(f"type = ${idx}")
        query_args.append(str(absence_type).strip().lower())
        idx += 1

    where_clause = " AND ".join(conditions)

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, absence_id, employee_id, namespace_id, type,
                   start_date, end_date, days, reason, status, compliance_state,
                   hr_source_id, raw, created_at
            FROM absences
            WHERE {where_clause}
            ORDER BY start_date DESC
            """,
            *query_args,
        )

    out = []
    for r in rows:
        is_self = caller_emp_id and r["employee_id"] == caller_emp_id
        can_read_reason = is_privileged or is_self

        st_d = r["start_date"]
        en_d = r["end_date"]
        raw_dict = json.loads(r["raw"]) if isinstance(r["raw"], str) else dict(r["raw"] or {})

        out.append(
            {
                "absence_id": r["absence_id"],
                "employee_id": r["employee_id"],
                "absence_type": r["type"],
                "type": r["type"],
                "start_date": st_d.isoformat() if st_d else None,
                "end_date": en_d.isoformat() if en_d else None,
                "days": float(r["days"]),
                "status": r["status"],
                "compliance_state": r["compliance_state"],
                "reason": r["reason"] if can_read_reason else None,
                "compliance": raw_dict.get("compliance") if can_read_reason else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    return {
        "namespace_id": str(ns_uuid),
        "count": len(out),
        "absences": out,
    }
