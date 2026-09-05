"""
nce/vertical_modules/hr/onboarding.py
=====================================
Onboarding quest templates, progression tracking, and task state management
for Module 13 (HR Engine).

Functions:
  - do_build_onboarding_quest: Generates, retrieves, or updates structured 90-day onboarding quests.
  - do_get_onboarding_progress: Retrieves individual onboarding progress, next tasks, and milestones.

Complies with:
  - Charter U4: Embedded in code rather than nce/config_data/**.
  - Charter U3: Identity scrub (neutral fixtures, no customer/person names).
  - RL-1: Individual progress only -- strictly NEVER peer ranking or score comparison.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.hr.onboarding")

_ROLE_QUEST_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "technician": [
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
    ],
    "project_manager": [
        {
            "stage": "stage_1_pm_setup",
            "title": "Days 1-14: Methodology, Systems & Handover Standards",
            "target_days": 14,
            "tasks": [
                "Complete project delivery methodology training",
                "Set up ERP, NCE project workspace, and scheduling permissions",
                "Review active delivery portfolio and critical path baselines",
                "Establish initial 1-on-1 cadence with field operations lead",
            ],
        },
        {
            "stage": "stage_2_vendor_contractor",
            "title": "Days 15-30: Vendor, Procurement & Subcontractor Coordination",
            "target_days": 30,
            "tasks": [
                "Shadow project handover meeting from Sales Engineering",
                "Coordinate materials staging and procurement timelines with warehouse",
                "Conduct contractor safety and scope alignment check",
            ],
        },
        {
            "stage": "stage_3_project_ownership",
            "title": "Days 31-60: Live Project Budget & Change Management",
            "target_days": 60,
            "tasks": [
                "Assume primary PM ownership of first commercial installation",
                "Process first site change order and customer variance sign-off",
                "Complete 30-day PM milestone review with Director of Operations",
            ],
        },
        {
            "stage": "stage_4_portfolio_delivery",
            "title": "Days 61-90: Multi-Site Delivery & Financial Reconciliation",
            "target_days": 90,
            "tasks": [
                "Deliver final commissioning acceptance document on primary project",
                "Conduct post-mortem review and gross margin financial reconciliation",
                "Complete 90-day review and establish quarterly project targets",
            ],
        },
    ],
    "sales_engineer": [
        {
            "stage": "stage_1_catalog_pricing",
            "title": "Days 1-14: Product Architecture, Catalog & Margins",
            "target_days": 14,
            "tasks": [
                "Review core AV technology stack and manufacturer partnerships",
                "Complete pricing engine and gross margin governance training",
                "Set up CRM, quote builder, and Dealroom workspace",
            ],
        },
        {
            "stage": "stage_2_bom_discovery",
            "title": "Days 15-30: Technical Discovery & Bill of Materials",
            "target_days": 30,
            "tasks": [
                "Participate in 3 customer technical discovery site walks",
                "Draft comprehensive bill of materials (BOM) for commercial boardroom",
                "Validate cable schedule and signal flow with Lead Engineer",
            ],
        },
        {
            "stage": "stage_3_proposal_pitch",
            "title": "Days 31-60: Proposal Creation & Customer Defense",
            "target_days": 60,
            "tasks": [
                "Co-lead enterprise customer proposal presentation and technical defense",
                "Integrate rebate and tier discount optimization into quote baseline",
                "Complete 30-day commercial onboarding review",
            ],
        },
        {
            "stage": "stage_4_deal_closing",
            "title": "Days 61-90: Pipeline Autonomy & Sign-off Governance",
            "target_days": 90,
            "tasks": [
                "Autonomously author and close first qualified commercial proposal",
                "Execute clean project handover meeting to assigned Project Manager",
                "Complete 90-day probationary review and annual revenue pacing",
            ],
        },
    ],
    "default": [
        {
            "stage": "stage_1_welcome",
            "title": "Days 1-14: Orientation, Systems & Security",
            "target_days": 14,
            "tasks": [
                "Complete general company onboarding and workspace setup",
                "Review employee handbook and Norwegian compliance policies",
                "Complete basic information security and GDPR guidelines",
            ],
        },
        {
            "stage": "stage_2_team_integration",
            "title": "Days 15-30: Team Workflows & Shadowing",
            "target_days": 30,
            "tasks": [
                "Meet key team stakeholders across departments",
                "Complete departmental process and tooling orientation",
                "Review initial personal learning and growth objectives",
            ],
        },
        {
            "stage": "stage_3_core_contributions",
            "title": "Days 31-60: Core Tasks & Initial Contributions",
            "target_days": 60,
            "tasks": [
                "Take full ownership of designated routine responsibilities",
                "Conduct 30-day feedback check-in with manager",
            ],
        },
        {
            "stage": "stage_4_full_autonomy",
            "title": "Days 61-90: Autonomous Contribution & Development",
            "target_days": 90,
            "tasks": [
                "Demonstrate autonomous execution of role responsibilities",
                "Complete 90-day probationary review and ongoing growth roadmap",
            ],
        },
    ],
}


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


def _parse_date(val: Any) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val).strip())
    except Exception:
        return date.today()


def _get_template_stages(role: str) -> list[dict[str, Any]]:
    norm_role = (role or "").strip().lower().replace("-", "_").replace(" ", "_")
    if "tech" in norm_role or "field" in norm_role or "installer" in norm_role:
        return _ROLE_QUEST_TEMPLATES["technician"]
    if "project" in norm_role or "pm" in norm_role or "manager" in norm_role:
        return _ROLE_QUEST_TEMPLATES["project_manager"]
    if "sale" in norm_role or "account" in norm_role or "consult" in norm_role:
        return _ROLE_QUEST_TEMPLATES["sales_engineer"]
    return _ROLE_QUEST_TEMPLATES.get(norm_role, _ROLE_QUEST_TEMPLATES["default"])


async def do_build_onboarding_quest(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Generate, retrieve, or update a structured 90-day onboarding quest for an employee.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (required) Target employee ID.
        - role: (optional) Role specialization (technician, project_manager, sales_engineer, default).
        - department: (optional) Department.
        - start_date: (optional, default today) Start date string (YYYY-MM-DD).
        - complete_task_id: (optional) Single task ID to mark completed.
        - completed_task_ids: (optional) List of task IDs to mark completed.
        - uncomplete_task_id: (optional) Task ID to mark uncompleted.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    explicit_role = params.get("role")
    explicit_dept = params.get("department")
    start_date = _parse_date(params.get("start_date"))

    complete_task_id = params.get("complete_task_id")
    completed_task_ids = list(params.get("completed_task_ids") or [])
    if complete_task_id:
        completed_task_ids.append(str(complete_task_id).strip())
    uncomplete_task_id = (
        str(params.get("uncomplete_task_id") or "").strip()
        if params.get("uncomplete_task_id")
        else None
    )

    stored_quest: dict[str, Any] | None = None
    emp_role = explicit_role
    emp_dept = explicit_dept
    emp_name = "Employee"

    # Try reading existing employee profile & stored quest state from database
    try:
        async with scoped_pg_session(pool, ns_uuid) as conn:
            row = await conn.fetchrow(
                """
                SELECT employee_id, namespace_id, name, role, department, raw
                FROM employees
                WHERE employee_id = $1 AND namespace_id = $2::uuid
                """,
                employee_id,
                ns_uuid,
            )
            if row:
                emp_name = row["name"]
                if not emp_role:
                    emp_role = row["role"]
                if not emp_dept:
                    emp_dept = row["department"]
                raw = (
                    json.loads(row["raw"])
                    if isinstance(row["raw"], str)
                    else dict(row["raw"] or {})
                )
                stored_quest = raw.get("onboarding_quest")
    except Exception as exc:
        log.debug("Database lookup for employee quest skipped or unavailable: %s", exc)

    role = str(emp_role or "technician").strip()
    department = str(emp_dept or "operations").strip()

    # Reconstruct or load quest
    if stored_quest and isinstance(stored_quest, dict):
        stages_data = list(stored_quest.get("stages") or [])
        q_start_str = stored_quest.get("start_date")
        if q_start_str:
            start_date = _parse_date(q_start_str)
    else:
        # Build fresh from template
        template_stages = _get_template_stages(role)
        stages_data = []
        for s in template_stages:
            deadline = (start_date + timedelta(days=s["target_days"])).isoformat()
            task_list = [
                {
                    "task_id": f"{s['stage']}_t{idx + 1}",
                    "description": desc,
                    "completed": False,
                    "completed_at": None,
                }
                for idx, desc in enumerate(s["tasks"])
            ]
            stages_data.append(
                {
                    "stage_id": s["stage"],
                    "title": s["title"],
                    "target_days": s["target_days"],
                    "deadline": deadline,
                    "tasks": task_list,
                }
            )

    # Apply task completions / uncompletions
    now_iso = datetime.utcnow().isoformat()
    total_tasks = 0
    completed_count = 0
    today = date.today()

    for stage in stages_data:
        stage_completed_tasks = 0
        tasks = stage.get("tasks", [])
        for task in tasks:
            tid = task.get("task_id")
            if tid in completed_task_ids:
                task["completed"] = True
                if not task.get("completed_at"):
                    task["completed_at"] = now_iso
            elif uncomplete_task_id and tid == uncomplete_task_id:
                task["completed"] = False
                task["completed_at"] = None

            if task.get("completed"):
                completed_count += 1
                stage_completed_tasks += 1
            total_tasks += 1

        stage_deadline_str = stage.get("deadline")
        stage_deadline = _parse_date(stage_deadline_str) if stage_deadline_str else today

        if len(tasks) > 0 and stage_completed_tasks == len(tasks):
            stage["status"] = "completed"
        elif stage_deadline < today and stage_completed_tasks < len(tasks):
            stage["status"] = "overdue"
        elif stage_completed_tasks > 0 or today <= stage_deadline:
            stage["status"] = "in_progress"
        else:
            stage["status"] = "pending"

    progress_pct = round((completed_count / total_tasks * 100.0), 1) if total_tasks > 0 else 0.0

    quest_result = {
        "namespace_id": str(ns_uuid),
        "employee_id": employee_id,
        "employee_name": emp_name,
        "role": role,
        "department": department,
        "start_date": start_date.isoformat(),
        "total_tasks": total_tasks,
        "completed_tasks": completed_count,
        "progress_pct": progress_pct,
        "stages": stages_data,
        "updated_at": now_iso,
    }

    # Persist updated quest back to employee profile if DB is connected
    try:
        async with scoped_pg_session(pool, ns_uuid) as conn:
            row = await conn.fetchrow(
                "SELECT raw FROM employees WHERE employee_id = $1 AND namespace_id = $2::uuid",
                employee_id,
                ns_uuid,
            )
            if row:
                raw_dict = (
                    json.loads(row["raw"])
                    if isinstance(row["raw"], str)
                    else dict(row["raw"] or {})
                )
                raw_dict["onboarding_quest"] = quest_result
                await conn.execute(
                    """
                    UPDATE employees
                    SET raw = $1::jsonb, updated_at = now()
                    WHERE employee_id = $2 AND namespace_id = $3::uuid
                    """,
                    json.dumps(raw_dict),
                    employee_id,
                    ns_uuid,
                )
    except Exception as exc:
        log.debug("Persisting quest state to employees table skipped: %s", exc)

    return quest_result


async def do_get_onboarding_progress(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve self-scoped progress summary and next recommended actions for onboarding."""
    quest = await do_build_onboarding_quest(engine, params)

    next_task = None
    active_stage = None
    overdue_tasks = []

    for stage in quest.get("stages", []):
        for task in stage.get("tasks", []):
            if not task.get("completed"):
                if next_task is None:
                    next_task = {
                        "stage_id": stage.get("stage_id"),
                        "stage_title": stage.get("title"),
                        "task_id": task.get("task_id"),
                        "description": task.get("description"),
                        "deadline": stage.get("deadline"),
                    }
                if stage.get("status") == "overdue":
                    overdue_tasks.append(
                        {
                            "task_id": task.get("task_id"),
                            "description": task.get("description"),
                            "stage": stage.get("title"),
                            "deadline": stage.get("deadline"),
                        }
                    )
        if stage.get("status") in ("in_progress", "overdue") and active_stage is None:
            active_stage = stage.get("title")

    return {
        "namespace_id": quest["namespace_id"],
        "employee_id": quest["employee_id"],
        "employee_name": quest["employee_name"],
        "role": quest["role"],
        "progress_pct": quest["progress_pct"],
        "completed_tasks": quest["completed_tasks"],
        "total_tasks": quest["total_tasks"],
        "active_stage": active_stage or "Completed",
        "next_task": next_task,
        "overdue_tasks": overdue_tasks,
    }
