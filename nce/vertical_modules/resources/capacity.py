"""
nce.vertical_modules.resources.capacity
========================================
Capacity calendar and availability resolver for Module 15 (Staff & Resources Engine).
Pure read operation providing capacity overview across resources and time windows.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.registry import VALID_RESOURCE_KINDS, _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.capacity")


async def do_resolve_capacity(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve available capacity and utilization across resources in a time window.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - window: (required) dict with 'starts_at' and 'ends_at'.
        - kind / resource_type: (optional) Filter by resource kind.
        - skill: (optional) Filter by required skill.
        - location: (optional) Filter by location.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    window = params.get("window")
    if not isinstance(window, dict) or not window.get("starts_at") or not window.get("ends_at"):
        raise ResourceValidationError("window with 'starts_at' and 'ends_at' is required.")

    kind = params.get("kind") or params.get("resource_type")
    if kind:
        kind = str(kind).strip().lower()
        if kind not in VALID_RESOURCE_KINDS:
            raise ResourceValidationError(f"Invalid resource_type: {kind!r}")

    pool = _extract_pool(engine)
    async with scoped_pg_session(pool, ns_id) as conn:
        if kind:
            rows = await conn.fetch(
                """
                SELECT id, namespace_id, kind, ref_id, display_name, attrs
                FROM resources
                WHERE namespace_id = $1 AND kind = $2
                ORDER BY display_name ASC
                """,
                ns_id,
                kind,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, namespace_id, kind, ref_id, display_name, attrs
                FROM resources
                WHERE namespace_id = $1
                ORDER BY display_name ASC
                """,
                ns_id,
            )

    total_resources = len(rows)
    available_resources: list[dict[str, Any]] = []
    utilized_resources: list[dict[str, Any]] = []
    total_util_pct = 0.0

    alloc_map: dict[str, int] = {}
    try:
        async with scoped_pg_session(pool, ns_id) as conn:
            alloc_rows = await conn.fetch(
                """
                SELECT resource_id, COUNT(*) AS alloc_count
                FROM allocations
                WHERE namespace_id = $1
                  AND status <> 'released'
                  AND tstzrange(starts_at, ends_at) && tstzrange($2::timestamptz, $3::timestamptz)
                GROUP BY resource_id
                """,
                ns_id,
                window.get("starts_at"),
                window.get("ends_at"),
            )
            for ar in alloc_rows:
                alloc_map[str(ar["resource_id"])] = int(ar["alloc_count"])
    except Exception:
        pass

    for r in rows:
        rid = str(r["id"])
        raw_attrs = (
            r["attrs"]
            if isinstance(r, dict) or hasattr(r, "__getitem__")
            else getattr(r, "attrs", None)
        )
        if isinstance(raw_attrs, str):
            try:
                import json

                attrs = json.loads(raw_attrs)
            except Exception:
                attrs = {}
        elif isinstance(raw_attrs, dict):
            attrs = raw_attrs
        else:
            attrs = {}

        u_pct = 0.0
        if "utilization_pct" in attrs:
            try:
                u_pct = float(attrs["utilization_pct"])
            except (ValueError, TypeError):
                u_pct = 0.0
        elif "allocated_hours" in attrs and "capacity_hours" in attrs:
            try:
                cap = float(attrs["capacity_hours"])
                alloc = float(attrs["allocated_hours"])
                if cap > 0:
                    u_pct = round((alloc / cap) * 100.0, 2)
            except (ValueError, TypeError):
                u_pct = 0.0
        elif alloc_map.get(rid, 0) > 0:
            u_pct = 100.0

        u_pct = max(0.0, min(100.0, u_pct))
        total_util_pct += u_pct

        res_info = {
            "id": rid,
            "kind": r["kind"],
            "ref_id": r["ref_id"],
            "display_name": r["display_name"],
            "status": "available" if u_pct < 100.0 else "fully_utilized",
            "utilization_pct": u_pct,
        }

        if u_pct > 0.0:
            utilized_resources.append(res_info)
        if u_pct < 100.0:
            available_resources.append(
                {
                    "id": rid,
                    "kind": r["kind"],
                    "ref_id": r["ref_id"],
                    "display_name": r["display_name"],
                    "status": "available",
                }
            )

    avg_utilization_pct = round(total_util_pct / total_resources, 2) if total_resources > 0 else 0.0

    return {
        "window": window,
        "total_resources": total_resources,
        "available_resources": available_resources,
        "utilized_resources": utilized_resources,
        "utilization_pct": avg_utilization_pct,
    }


do_check_capacity = do_resolve_capacity
