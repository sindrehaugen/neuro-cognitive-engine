"""
nce.vertical_modules.resources.forecast
========================================
Demand Forecasting & Capacity Gap Intelligence for Module 15 (Staff & Resources Engine).
Forecasts resource supply vs committed allocations and pipeline demand across planning horizons.
Generates hiring / contractor surge signals (Advisor role) and Morning Brief capacity pulses.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.registry import _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.forecast")


async def do_forecast_demand(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Forecast staff & resource demand vs capacity across a specified horizon.
    Returns:
      - Supply: available hours from active resources
      - Demand: committed hours + pipeline project demands
      - Net Capacity Gap (hours)
      - Capacity Status: 'deficit', 'surplus', 'balanced'
      - Actionable Recommendations: hire / contractor surge signals
    """
    require_resources_enabled(params.get("namespace_metadata"))
    pool = _extract_pool(engine)

    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    raw_horizon = params.get("horizon_days")
    horizon_days = int(raw_horizon) if raw_horizon is not None else 30
    if horizon_days <= 0 or horizon_days > 365:
        raise ResourceValidationError("horizon_days must be between 1 and 365.")

    role_filter = params.get("role") or params.get("kind")
    skills_filter = params.get("skills") or []
    if isinstance(skills_filter, str):
        skills_filter = [skills_filter]

    start_dt = datetime.now(timezone.utc)
    end_dt = start_dt + timedelta(days=horizon_days)

    working_days = max(1, int(horizon_days * (5 / 7)))
    hours_per_day = 7.5

    async with scoped_pg_session(pool, ns_id) as conn:
        # 1. Fetch active resources
        res_rows = await conn.fetch(
            """
            SELECT id, name, kind, capacity_pct, metadata
            FROM resources
            WHERE namespace_id = $1 AND active = true
            """,
            ns_id,
        )

        active_resources = []
        for r in res_rows:
            meta = (
                json.loads(r["metadata"])
                if isinstance(r["metadata"], str)
                else (r["metadata"] or {})
            )
            if role_filter and r["kind"] != role_filter and meta.get("role") != role_filter:
                continue
            if skills_filter:
                res_skills = meta.get("skills") or []
                if not any(s in res_skills for s in skills_filter):
                    continue
            active_resources.append(
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "kind": r["kind"],
                    "capacity_pct": float(r["capacity_pct"]),
                    "metadata": meta,
                }
            )

        total_available_hours = sum(
            (res["capacity_pct"] / 100.0) * hours_per_day * working_days for res in active_resources
        )

        # 2. Fetch existing committed allocations within horizon
        res_ids = [res["id"] for res in active_resources]
        committed_hours = 0.0

        if res_ids:
            alloc_rows = await conn.fetch(
                """
                SELECT id, resource_id, starts_at, ends_at
                FROM allocations
                WHERE namespace_id = $1
                  AND resource_id = ANY($2::uuid[])
                  AND status <> 'released'
                  AND tstzrange(starts_at, ends_at) && tstzrange($3, $4)
                """,
                ns_id,
                res_ids,
                start_dt,
                end_dt,
            )
            for ar in alloc_rows:
                s = max(ar["starts_at"], start_dt)
                e = min(ar["ends_at"], end_dt)
                if e > s:
                    duration_hrs = (e - s).total_seconds() / 3600.0
                    committed_hours += duration_hrs

        # 3. Calculate pipeline demand
        pipeline_demands = params.get("pipeline_demands") or []
        pipeline_hours = 0.0

        if pipeline_demands:
            for d in pipeline_demands:
                pipeline_hours += float(d.get("hours") or d.get("estimated_hours") or 0.0)
        else:
            # Check for planned projects or project tasks in graph
            try:
                task_rows = await conn.fetch(
                    """
                    SELECT label, raw
                    FROM kg_nodes
                    WHERE namespace_id = $1
                      AND entity_type = 'PROJECT_TASK'
                      AND (raw->>'status' = 'planned' OR raw->>'status' = 'scheduled')
                    LIMIT 100
                    """,
                    ns_id,
                )
                for tr in task_rows:
                    raw = json.loads(tr["raw"]) if isinstance(tr["raw"], str) else (tr["raw"] or {})
                    pipeline_hours += float(raw.get("estimated_hours") or 16.0)
            except Exception as exc:
                log.debug("No kg_nodes pipeline task query: %s", exc)

        # 4. Compute metrics
        total_demand_hours = committed_hours + pipeline_hours
        net_capacity_hours = total_available_hours - total_demand_hours
        capacity_gap_hours = max(0.0, -net_capacity_hours)
        utilization_pct = (
            round((total_demand_hours / total_available_hours) * 100.0, 1)
            if total_available_hours > 0
            else 100.0
        )

        recommendations = []
        if capacity_gap_hours > 0:
            status = "deficit"
            needed_techs = math.ceil(capacity_gap_hours / (working_days * hours_per_day))
            recommendations.append(
                f"Capacity deficit of {capacity_gap_hours:.1f} hours detected over {horizon_days}-day horizon."
            )
            recommendations.append(
                f"Recommend engaging {needed_techs} contractor technician(s) or opening hiring requisition to avoid project delivery delays."
            )
        elif utilization_pct < 60.0:
            status = "surplus"
            recommendations.append(
                f"Capacity surplus of {net_capacity_hours:.1f} hours ({utilization_pct}% utilization). Capacity available for Sales pipeline pull-forward."
            )
        else:
            status = "balanced"
            recommendations.append(
                f"Resource capacity is balanced at {utilization_pct}% utilization across {len(active_resources)} active resource(s)."
            )

        return {
            "namespace_id": str(ns_id),
            "horizon_days": horizon_days,
            "working_days": working_days,
            "active_resources_count": len(active_resources),
            "available_capacity_hours": round(total_available_hours, 1),
            "committed_allocation_hours": round(committed_hours, 1),
            "pipeline_demand_hours": round(pipeline_hours, 1),
            "total_demand_hours": round(total_demand_hours, 1),
            "net_capacity_hours": round(net_capacity_hours, 1),
            "capacity_gap_hours": round(capacity_gap_hours, 1),
            "utilization_pct": utilization_pct,
            "status": status,
            "recommendations": recommendations,
            "role_filter": role_filter,
            "skills_filter": skills_filter,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


async def get_morning_brief_capacity_pulse(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Compute daily capacity health pulse for the Morning Brief.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    pool = _extract_pool(engine)
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    async with scoped_pg_session(pool, ns_id) as conn:
        res_rows = await conn.fetch(
            "SELECT id, kind FROM resources WHERE namespace_id = $1 AND active = true",
            ns_id,
        )
        total_active = len(res_rows)

        alloc_rows = await conn.fetch(
            """
            SELECT DISTINCT resource_id
            FROM allocations
            WHERE namespace_id = $1
              AND status <> 'released'
              AND tstzrange(starts_at, ends_at) && tstzrange($2, $3)
            """,
            ns_id,
            day_start,
            day_end,
        )
        allocated_today = len(alloc_rows)

    utilization_pct = (
        round((allocated_today / total_active) * 100.0, 1) if total_active > 0 else 0.0
    )

    if utilization_pct > 95.0:
        health = "saturated"
    elif utilization_pct < 40.0:
        health = "underutilized"
    else:
        health = "optimal"

    return {
        "namespace_id": str(ns_id),
        "date": day_start.date().isoformat(),
        "total_active_resources": total_active,
        "allocated_today_resources": allocated_today,
        "available_today_resources": max(0, total_active - allocated_today),
        "daily_utilization_pct": utilization_pct,
        "capacity_health": health,
        "generated_at": now.isoformat(),
    }
