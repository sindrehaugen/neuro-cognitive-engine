"""
nce/vertical_modules/hr/onboarding.py
=====================================
Onboarding quest templates and progress generation for Module 13.

Functions:
  - do_build_onboarding_quest: Generates a structured 90-day onboarding checklist.

Complies with Charter U4: Embedded in code rather than nce/config_data/**.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from uuid import UUID

log = logging.getLogger("nce.vertical_modules.hr.onboarding")

_STANDARD_90_DAY_STAGES = [
    {
        "stage": "stage_1_safety_tools",
        "title": "Days 1-14: Safety, Access & Basic Tooling",
        "target_days": 14,
        "tasks": [
            "Complete company HSE and safety briefing",
            "Verify PPE (hard hat, safety shoes, hi-vis vest)",
            "Issue access card, laptop, and building permissions",
            "Verify calibration and inspection of standard technician tool kit",
        ],
    },
    {
        "stage": "stage_2_shadowing_certs",
        "title": "Days 15-30: Mentored Field Shadowing & Foundation Certs",
        "target_days": 30,
        "tasks": [
            "Complete Dante Level 1 certification",
            "Shadow Lead Field Technician on 3 commercial client sites",
            "Demonstrate clean rack dress and structured cable termination",
            "Review AVIXA CTS fundamentals and study curriculum",
        ],
    },
    {
        "stage": "stage_3_minor_work_orders",
        "title": "Days 31-60: Supervised Service & Minor Commissions",
        "target_days": 60,
        "tasks": [
            "Lead first minor service work order with mentor co-sign",
            "Successfully configure AV-over-IP endpoints (DM-NVX / Q-SYS)",
            "Complete 30-day onboarding feedback review with manager",
        ],
    },
    {
        "stage": "stage_4_independent_work",
        "title": "Days 61-90: Independent Commissioning & Milestone Review",
        "target_days": 90,
        "tasks": [
            "Independently execute room commissioning and sign-off checklist",
            "Achieve formal CTS or manufacturer core cert",
            "Complete 90-day probationary review and ongoing development plan",
        ],
    },
]


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


async def do_build_onboarding_quest(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Generate or retrieve a structured 90-day onboarding quest for an employee.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Target employee ID.
        - role: (optional, default 'technician') Role specialization.
        - department: (optional, default 'operations') Department.
        - start_date: (optional, default today) Start date string (YYYY-MM-DD).
    """
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    role = str(params.get("role") or "technician").strip()
    department = str(params.get("department") or "operations").strip()

    start_date_str = params.get("start_date")
    if start_date_str:
        try:
            start_date = date.fromisoformat(str(start_date_str).strip())
        except Exception:
            start_date = date.today()
    else:
        start_date = date.today()

    stages_out: list[dict[str, Any]] = []
    total_tasks = 0

    for s in _STANDARD_90_DAY_STAGES:
        deadline = (start_date + timedelta(days=s["target_days"])).isoformat()
        task_list = [
            {
                "task_id": f"{s['stage']}_t{idx + 1}",
                "description": desc,
                "completed": False,
            }
            for idx, desc in enumerate(s["tasks"])
        ]
        total_tasks += len(task_list)
        stages_out.append(
            {
                "stage_id": s["stage"],
                "title": s["title"],
                "deadline": deadline,
                "tasks": task_list,
            }
        )

    return {
        "namespace_id": str(ns_uuid),
        "employee_id": employee_id,
        "role": role,
        "department": department,
        "start_date": start_date.isoformat(),
        "total_tasks": total_tasks,
        "completed_tasks": 0,
        "progress_pct": 0.0,
        "stages": stages_out,
    }
