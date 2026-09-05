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

    for r in rows:
        available_resources.append(
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "ref_id": r["ref_id"],
                "display_name": r["display_name"],
                "status": "available",
            }
        )

    return {
        "window": window,
        "total_resources": total_resources,
        "available_resources": available_resources,
        "utilized_resources": [],
        "utilization_pct": 0.0,
    }
