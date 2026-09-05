"""
nce/vertical_modules/hr/profile.py
==================================
Employee profile management for Module 13 (HR Engine).

Functions:
  - do_create_employee: Insert or register a new employee card.
  - do_get_employee: Retrieve profile card, skills, and active certs (field-scoped).
  - do_query_employees: Search/filter active employees with tenant predicates.
  - do_update_employee: Update employee metadata.

Access Scoping:
  - Self, Admin, Manager, and HR callers receive full profiles (including leave_balance).
  - Peers receive public profile views only (sensitive fields stripped).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.hr.profile")

EVENT_TYPE_HR_EMPLOYEE_CREATED: str = "hr_employee_created"


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


async def do_create_employee(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create or register an employee profile card.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Unique identifier for the employee.
        - name: (required) Full name.
        - email: (optional) Email address.
        - role: (optional, default 'technician') e.g. technician, project_lead, engineer.
        - department: (optional, default 'operations') e.g. operations, engineering, sales.
        - location_id: (optional) Geographic or base location ID.
        - leave_balance: (optional, default 25.0) Annual leave balance in days.
        - active: (optional, default true) Whether employee is active.
        - hr_source_id: (optional) Upstream source ID for GDPR hard retirement.
        - raw: (optional) Additional metadata dict.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    name = str(params.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")

    email = params.get("email")
    role = str(params.get("role") or "technician").strip()
    department = str(params.get("department") or "operations").strip()
    location_id = params.get("location_id")
    leave_balance = float(
        params.get("leave_balance") if params.get("leave_balance") is not None else 25.0
    )
    active = bool(params.get("active", True))
    hr_source_id = params.get("hr_source_id")

    raw = params.get("raw") or {}
    raw_json = json.dumps(raw) if not isinstance(raw, str) else raw

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO employees (
                employee_id, namespace_id, name, email, role, department,
                location_id, leave_balance, active, raw, hr_source_id
            )
            VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
            ON CONFLICT (employee_id, namespace_id) DO UPDATE
            SET name = EXCLUDED.name,
                email = COALESCE(EXCLUDED.email, employees.email),
                role = EXCLUDED.role,
                department = EXCLUDED.department,
                location_id = COALESCE(EXCLUDED.location_id, employees.location_id),
                leave_balance = EXCLUDED.leave_balance,
                active = EXCLUDED.active,
                raw = EXCLUDED.raw,
                hr_source_id = COALESCE(EXCLUDED.hr_source_id, employees.hr_source_id),
                updated_at = now()
            RETURNING id, employee_id, namespace_id, name, email, role, department,
                      location_id, leave_balance, active, hr_source_id, created_at, updated_at
            """,
            employee_id,
            ns_uuid,
            name,
            email,
            role,
            department,
            location_id,
            leave_balance,
            active,
            raw_json,
            hr_source_id,
        )

    return {
        "id": str(row["id"]),
        "employee_id": row["employee_id"],
        "namespace_id": str(row["namespace_id"]),
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "department": row["department"],
        "location_id": row["location_id"],
        "leave_balance": float(row["leave_balance"]),
        "active": row["active"],
        "hr_source_id": row["hr_source_id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def do_get_employee(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve an employee card, associated skills, and active certifications.

    Enforces field-level access scoping:
    - Manager, Admin, HR, or Self: full card with leave_balance and hr_source_id.
    - Peer: public card only (leave_balance and sensitive metadata omitted).
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    caller_role = str(params.get("caller_role") or "peer").strip().lower()
    caller_id = str(params.get("caller_id") or "").strip()
    is_privileged = caller_role in ("admin", "manager", "hr") or (
        caller_id and caller_id == employee_id
    )

    async with scoped_pg_session(pool, ns_uuid) as conn:
        emp = await conn.fetchrow(
            """
            SELECT id, employee_id, namespace_id, name, email, role, department,
                   location_id, leave_balance, active, hr_source_id, raw,
                   created_at, updated_at
            FROM   employees
            WHERE  employee_id = $1 AND namespace_id = $2::uuid
            """,
            employee_id,
            ns_uuid,
        )
        if not emp:
            raise ValueError(f"Employee {employee_id!r} not found in namespace.")

        skills = await conn.fetch(
            """
            SELECT skill_id, name, category, level, assessed_at
            FROM   skills
            WHERE  employee_id = $1 AND namespace_id = $2::uuid
            ORDER BY name ASC
            """,
            employee_id,
            ns_uuid,
        )

        certs = await conn.fetch(
            """
            SELECT cert_id, authority, name, issued, valid_to, status
            FROM   certifications
            WHERE  employee_id = $1 AND namespace_id = $2::uuid
            ORDER BY valid_to DESC NULLS LAST
            """,
            employee_id,
            ns_uuid,
        )

    card: dict[str, Any] = {
        "id": str(emp["id"]),
        "employee_id": emp["employee_id"],
        "namespace_id": str(emp["namespace_id"]),
        "name": emp["name"],
        "email": emp["email"],
        "role": emp["role"],
        "department": emp["department"],
        "location_id": emp["location_id"],
        "active": emp["active"],
        "skills": [
            {
                "skill_id": s["skill_id"],
                "name": s["name"],
                "category": s["category"],
                "level": s["level"],
                "assessed_at": s["assessed_at"].isoformat() if s["assessed_at"] else None,
            }
            for s in skills
        ],
        "certifications": [
            {
                "cert_id": c["cert_id"],
                "authority": c["authority"],
                "name": c["name"],
                "issued": c["issued"].isoformat() if c["issued"] else None,
                "valid_to": c["valid_to"].isoformat() if c["valid_to"] else None,
                "status": c["status"],
            }
            for c in certs
        ],
    }

    if is_privileged:
        card["leave_balance"] = float(emp["leave_balance"])
        card["hr_source_id"] = emp["hr_source_id"]
        raw_val = emp["raw"]
        if isinstance(raw_val, str):
            try:
                card["raw"] = json.loads(raw_val)
            except Exception:
                card["raw"] = {}
        else:
            card["raw"] = raw_val or {}

    return card


async def do_query_employees(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Query/filter employees in the active namespace.

    Field-scoped based on caller_role.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    caller_role = str(params.get("caller_role") or "peer").strip().lower()
    is_privileged = caller_role in ("admin", "manager", "hr")

    department = params.get("department")
    role = params.get("role")
    active = params.get("active")
    location_id = params.get("location_id")
    limit = max(1, min(100, int(params.get("limit") or 50)))
    offset = max(0, int(params.get("offset") or 0))

    query_parts = [
        "SELECT id, employee_id, namespace_id, name, email, role, department, location_id, leave_balance, active, created_at FROM employees WHERE namespace_id = $1::uuid"
    ]
    query_args: list[Any] = [ns_uuid]
    idx = 2

    if department:
        query_parts.append(f"AND department = ${idx}")
        query_args.append(str(department).strip())
        idx += 1

    if role:
        query_parts.append(f"AND role = ${idx}")
        query_args.append(str(role).strip())
        idx += 1

    if active is not None:
        query_parts.append(f"AND active = ${idx}")
        query_args.append(bool(active))
        idx += 1

    if location_id:
        query_parts.append(f"AND location_id = ${idx}")
        query_args.append(str(location_id).strip())
        idx += 1

    query_parts.append(f"ORDER BY name ASC LIMIT ${idx} OFFSET ${idx + 1}")
    query_args.extend([limit, offset])

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(" ".join(query_parts), *query_args)

    employees: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {
            "id": str(r["id"]),
            "employee_id": r["employee_id"],
            "namespace_id": str(r["namespace_id"]),
            "name": r["name"],
            "email": r["email"],
            "role": r["role"],
            "department": r["department"],
            "location_id": r["location_id"],
            "active": r["active"],
        }
        if is_privileged:
            item["leave_balance"] = float(r["leave_balance"])
        employees.append(item)

    return {
        "namespace_id": str(ns_uuid),
        "employees": employees,
        "count": len(employees),
    }
