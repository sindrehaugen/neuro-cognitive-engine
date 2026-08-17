"""
nce/vertical_modules/project/pl.py
===================================
Domain core: read-only advisor surfaces for PL and team capacity.

Features:
1. do_my_day(engine, params) -> dict:
   Rank open tasks by priority: gate-blocking * deadline * value.
2. do_capacity(engine, params) -> dict:
   Aggregate open task load per PL/team over a given window.

Both functions are read-only cores, respect RLS namespace isolation,
and have no external HR system dependencies.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.pl")


def parse_date(val: Any) -> datetime.date | None:
    """Parse a date from a value, handling optional prefixes like 'DEADLINE:'."""
    if not val:
        return None
    if isinstance(val, datetime.date):
        return val
    if isinstance(val, datetime.datetime):
        return val.date()
    s = str(val).strip()
    if s.upper().startswith("DEADLINE:"):
        s = s[9:].strip()
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None


def parse_float(val: Any, default: float = 1.0) -> float:
    """Parse a float from a value, handling optional prefixes like 'VALUE:', 'LOAD:', 'EFFORT:'."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    for prefix in ("VALUE:", "LOAD:", "EFFORT:"):
        if s.upper().startswith(prefix):
            s = s[len(prefix) :].strip()
    try:
        return float(s)
    except ValueError:
        return default


def calculate_priority(
    gate_blocking: bool,
    deadline: datetime.date | None,
    value: float,
    reference_date: datetime.date,
) -> float:
    """Calculate task priority: gate_blocking_factor * deadline_factor * value."""
    gate_blocking_factor = 1.5 if gate_blocking else 1.0

    if deadline is None:
        deadline_factor = 1.0
    else:
        days_diff = (deadline - reference_date).days
        if days_diff <= 0:
            deadline_factor = 10.0
        else:
            deadline_factor = 1.0 / (days_diff + 1)

    return gate_blocking_factor * deadline_factor * value


async def do_my_day(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve and rank open tasks by priority (gate-blocking * deadline * value)."""
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_my_day: 'namespace_id' is required"}
    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except (ValueError, AttributeError) as exc:
        return {"ok": False, "error": f"do_my_day: invalid namespace_id: {exc}"}

    ref_date_raw = params.get("reference_date") or params.get("current_date")
    reference_date = parse_date(ref_date_raw) or datetime.date.today()

    try:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            # 1. Fetch all open tasks (PROJECT_TASK nodes with incoming 'generates' edge)
            task_rows = await conn.fetch(
                """
                SELECT DISTINCT n.label
                FROM kg_nodes n
                JOIN kg_edges e ON e.object_label = n.label AND e.namespace_id = n.namespace_id
                WHERE n.entity_type = 'PROJECT_TASK'
                  AND e.predicate = 'generates'
                  AND n.namespace_id = $1::uuid
                """,
                str(ns_uuid),
            )
            task_labels = [row["label"] for row in task_rows]
            if not task_labels:
                return {"ok": True, "tasks": []}

            # 2. Fetch all properties/edges for these open tasks in a single query
            edge_rows = await conn.fetch(
                """
                SELECT subject_label, predicate, object_label
                FROM kg_edges
                WHERE subject_label = ANY($1::text[])
                  AND namespace_id = $2::uuid
                """,
                task_labels,
                str(ns_uuid),
            )

            # Group edges by task
            task_data: dict[str, dict[str, Any]] = {
                label: {
                    "gate_blocking": False,
                    "deadline": None,
                    "value": 1.0,
                }
                for label in task_labels
            }

            for row in edge_rows:
                task_lbl = row["subject_label"]
                pred = row["predicate"].lower()
                obj = row["object_label"]

                if task_lbl not in task_data:
                    continue

                if pred in ("is_gate_blocking", "gate_blocking"):
                    task_data[task_lbl]["gate_blocking"] = obj.lower() in ("true", "1", "yes")
                elif pred in ("deadline", "has_deadline"):
                    task_data[task_lbl]["deadline"] = parse_date(obj)
                elif pred in ("value", "has_value"):
                    task_data[task_lbl]["value"] = parse_float(obj, 1.0)

            # 3. Calculate priorities and rank tasks
            ranked_tasks = []
            for label, data in task_data.items():
                p = calculate_priority(
                    gate_blocking=data["gate_blocking"],
                    deadline=data["deadline"],
                    value=data["value"],
                    reference_date=reference_date,
                )
                ranked_tasks.append(
                    {
                        "task_label": label,
                        "priority": p,
                        "gate_blocking": data["gate_blocking"],
                        "deadline": data["deadline"].isoformat() if data["deadline"] else None,
                        "value": data["value"],
                    }
                )

            # Sort by priority desc, then alphabetically by task_label for deterministic results
            ranked_tasks.sort(key=lambda t: (-t["priority"], t["task_label"]))

            return {
                "ok": True,
                "tasks": ranked_tasks,
            }

    except Exception as exc:
        log.error("do_my_day: failed to process ns=%s: %s", ns_uuid, exc, exc_info=True)
        return {"ok": False, "error": f"do_my_day: failed to process: {exc}"}


async def do_capacity(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate open task load per PL/team over a given window."""
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_capacity: 'namespace_id' is required"}
    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except (ValueError, AttributeError) as exc:
        return {"ok": False, "error": f"do_capacity: invalid namespace_id: {exc}"}

    start_date = parse_date(params.get("start_date"))
    end_date = parse_date(params.get("end_date"))

    try:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            # 1. Fetch all open tasks
            task_rows = await conn.fetch(
                """
                SELECT DISTINCT n.label
                FROM kg_nodes n
                JOIN kg_edges e ON e.object_label = n.label AND e.namespace_id = n.namespace_id
                WHERE n.entity_type = 'PROJECT_TASK'
                  AND e.predicate = 'generates'
                  AND n.namespace_id = $1::uuid
                """,
                str(ns_uuid),
            )
            task_labels = [row["label"] for row in task_rows]
            if not task_labels:
                return {"ok": True, "teams": {}}

            # 2. Fetch all properties/edges for these open tasks in a single query
            edge_rows = await conn.fetch(
                """
                SELECT subject_label, predicate, object_label
                FROM kg_edges
                WHERE subject_label = ANY($1::text[])
                  AND namespace_id = $2::uuid
                """,
                task_labels,
                str(ns_uuid),
            )

            # Group task properties
            task_data: dict[str, dict[str, Any]] = {
                label: {
                    "deadline": None,
                    "load": 1.0,
                    "assigned_to": None,
                }
                for label in task_labels
            }

            for row in edge_rows:
                task_lbl = row["subject_label"]
                pred = row["predicate"].lower()
                obj = row["object_label"]

                if task_lbl not in task_data:
                    continue

                if pred in ("deadline", "has_deadline"):
                    task_data[task_lbl]["deadline"] = parse_date(obj)
                elif pred in ("load", "has_load", "effort"):
                    task_data[task_lbl]["load"] = parse_float(obj, 1.0)
                elif pred in ("assigned_to", "assigned", "assignee"):
                    task_data[task_lbl]["assigned_to"] = obj

            # 3. Filter tasks by date window
            # If start_date or end_date are specified, we only include tasks with a deadline that falls in the window
            filtered_tasks = {}
            for label, data in task_data.items():
                deadline = data["deadline"]
                if start_date or end_date:
                    if deadline is None:
                        continue
                    if start_date and deadline < start_date:
                        continue
                    if end_date and deadline > end_date:
                        continue
                filtered_tasks[label] = data

            if not filtered_tasks:
                return {"ok": True, "teams": {}}

            # 4. Resolve teams for assigned employees
            # Find unique employees assigned
            employees = {
                data["assigned_to"] for data in filtered_tasks.values() if data["assigned_to"]
            }
            employee_teams: dict[str, str] = {}

            if employees:
                team_rows = await conn.fetch(
                    """
                    SELECT subject_label, object_label
                    FROM kg_edges
                    WHERE subject_label = ANY($1::text[])
                      AND predicate IN ('member_of', 'belongs_to', 'reports_to', 'team', 'pl')
                      AND namespace_id = $2::uuid
                    """,
                    list(employees),
                    str(ns_uuid),
                )
                for row in team_rows:
                    emp = row["subject_label"]
                    team = row["object_label"]
                    if emp not in employee_teams:
                        employee_teams[emp] = team

            # 5. Aggregate load per team
            teams_summary: dict[str, dict[str, Any]] = {}

            for label, data in filtered_tasks.items():
                emp = data["assigned_to"]
                team = employee_teams.get(emp, "Unassigned") if emp else "Unassigned"

                if team not in teams_summary:
                    teams_summary[team] = {
                        "total_load": 0.0,
                        "tasks": [],
                    }

                teams_summary[team]["total_load"] += data["load"]
                teams_summary[team]["tasks"].append(
                    {
                        "task_label": label,
                        "assigned_to": emp,
                        "load": data["load"],
                        "deadline": data["deadline"].isoformat() if data["deadline"] else None,
                    }
                )

            # Sort tasks within each team alphabetically by task_label for deterministic output
            for team_info in teams_summary.values():
                team_info["tasks"].sort(key=lambda t: t["task_label"])

            return {
                "ok": True,
                "teams": teams_summary,
            }

    except Exception as exc:
        log.error("do_capacity: failed to process ns=%s: %s", ns_uuid, exc, exc_info=True)
        return {"ok": False, "error": f"do_capacity: failed to process: {exc}"}
