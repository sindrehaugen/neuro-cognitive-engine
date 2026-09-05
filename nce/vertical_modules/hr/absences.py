"""
nce/vertical_modules/hr/absences.py
==================================
Absence and leave registration for Module 13 (HR Engine).

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

log = logging.getLogger("nce.vertical_modules.hr.absences")

_VALID_ABSENCE_TYPES = frozenset(
    {"vacation", "sick_leave", "parental", "compassionate", "training", "other"}
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

    absence_type = str(params.get("absence_type") or "").strip().lower()
    if absence_type not in _VALID_ABSENCE_TYPES:
        raise ValueError(
            f"absence_type must be one of {sorted(_VALID_ABSENCE_TYPES)}, got {absence_type!r}"
        )

    start_d = _parse_date(params.get("start_date"), "start_date")
    end_d = _parse_date(params.get("end_date"), "end_date")
    if end_d < start_d:
        raise ValueError(f"end_date ({end_d}) cannot be before start_date ({start_d})")

    absence_id = str(params.get("absence_id") or f"ABS-{uuid4().hex[:8].upper()}").strip()
    days_val = params.get("days")
    if days_val is not None:
        days = float(days_val)
    else:
        # Simple calendar day difference inclusive + 1
        days = float((end_d - start_d).days + 1)

    reason = params.get("reason")
    status = str(params.get("status") or "approved").strip().lower()
    hr_source_id = params.get("hr_source_id")
    raw = params.get("raw") or {}
    raw_json = json.dumps(raw) if not isinstance(raw, str) else raw

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO absences (
                absence_id, employee_id, namespace_id, absence_type,
                start_date, end_date, days, reason, status, hr_source_id, raw
            )
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (absence_id, namespace_id) DO UPDATE
            SET absence_type = EXCLUDED.absence_type,
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                days = EXCLUDED.days,
                reason = EXCLUDED.reason,
                status = EXCLUDED.status,
                hr_source_id = COALESCE(EXCLUDED.hr_source_id, absences.hr_source_id),
                raw = EXCLUDED.raw,
                updated_at = now()
            RETURNING id, absence_id, employee_id, namespace_id, absence_type,
                      start_date, end_date, days, reason, status, hr_source_id, created_at
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
            hr_source_id,
            raw_json,
        )

    return {
        "id": str(row["id"]),
        "absence_id": row["absence_id"],
        "employee_id": row["employee_id"],
        "namespace_id": str(row["namespace_id"]),
        "absence_type": row["absence_type"],
        "start_date": row["start_date"].isoformat() if row["start_date"] else None,
        "end_date": row["end_date"].isoformat() if row["end_date"] else None,
        "days": float(row["days"]),
        "status": row["status"],
        "hr_source_id": row["hr_source_id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
