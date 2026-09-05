"""
nce.vertical_modules.resources.allocations
==========================================
Resource allocation and double-booking conflict prevention for Module 15 (Staff & Resources Engine).
Enforces database-level concurrency conflict exclusion via PostgreSQL btree_gist extension (RS-3)
and contractor sub-scope allow-list redaction (Charter §6.4).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

try:
    import asyncpg
except ImportError:
    asyncpg = None

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceConcurrencyError,
    ResourceNotFoundError,
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.registry import _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.allocations")

VALID_ALLOCATION_STATUSES = frozenset({"tentative", "reserved", "confirmed", "released"})

# Contractor sub-scope allow-list per Charter §6.4 & Spec §100
# External contractors must never see margin, internal labor cost, pricing, or sensitive notes.
CONTRACTOR_ALLOWED_ALLOCATION_FIELDS = frozenset(
    {
        "id",
        "namespace_id",
        "resource_id",
        "demand_kind",
        "demand_id",
        "functional_location_id",
        "starts_at",
        "ends_at",
        "status",
        "confidence",
        "created_at",
        "updated_at",
    }
)


def _parse_datetime(val: Any, field_name: str) -> datetime:
    """Parse an ISO-8601 string or datetime into a UTC-aware datetime."""
    if not val:
        raise ResourceValidationError(f"{field_name} is required.")
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as exc:
        raise ResourceValidationError(f"Invalid {field_name} ISO datetime: {val!r}") from exc


def redact_contractor_view(allocation: dict[str, Any]) -> dict[str, Any]:
    """Strip internal margin, rates, pricing, and attrs for contractor sub-scope views."""
    redacted = {k: v for k, v in allocation.items() if k in CONTRACTOR_ALLOWED_ALLOCATION_FIELDS}
    redacted["redaction"] = "contractor_allow_list_enforced"
    return redacted


async def do_reserve(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Reserve a time window for a resource.
    DB-enforced against double-booking via PostgreSQL btree_gist exclusion constraint (RS-3).
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    res_id = _parse_uuid(params.get("resource_id"), "resource_id")

    starts_at = _parse_datetime(params.get("starts_at"), "starts_at")
    ends_at = _parse_datetime(params.get("ends_at"), "ends_at")
    if ends_at <= starts_at:
        raise ResourceValidationError(f"ends_at ({ends_at}) must be after starts_at ({starts_at}).")

    demand_kind = str(params.get("demand_kind") or "").strip()
    if not demand_kind:
        raise ResourceValidationError(
            "demand_kind is required (e.g. 'project', 'work_order', 'service')."
        )

    demand_id_raw = params.get("demand_id")
    demand_id = _parse_uuid(demand_id_raw, "demand_id") if demand_id_raw else None

    fl_id_raw = params.get("functional_location_id")
    functional_location_id = _parse_uuid(fl_id_raw, "functional_location_id") if fl_id_raw else None

    status = str(params.get("status") or "reserved").strip().lower()
    if status not in VALID_ALLOCATION_STATUSES:
        raise ResourceValidationError(
            f"Invalid allocation status: {status!r}. Valid: {sorted(VALID_ALLOCATION_STATUSES)}"
        )

    confidence = float(params.get("confidence", 1.0))
    if not (0.0 <= confidence <= 1.0):
        raise ResourceValidationError("confidence must be between 0.0 and 1.0.")

    attrs = params.get("attrs")
    if attrs is None:
        attrs = {}
    elif not isinstance(attrs, dict):
        raise ResourceValidationError("attrs must be a dictionary.")

    pool = _extract_pool(engine)
    alloc_id = uuid4()
    attrs_json = json.dumps(attrs)

    async with scoped_pg_session(pool, ns_id) as conn:
        # Verify resource exists within tenant scope
        res_row = await conn.fetchrow(
            "SELECT id, kind FROM resources WHERE id = $1 AND namespace_id = $2",
            res_id,
            ns_id,
        )
        if not res_row:
            raise ResourceNotFoundError(f"Resource {res_id} not found in namespace {ns_id}.")

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO allocations (
                    id, namespace_id, resource_id, demand_kind, demand_id,
                    functional_location_id, starts_at, ends_at, status, confidence, attrs
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                RETURNING id, namespace_id, resource_id, demand_kind, demand_id,
                          functional_location_id, starts_at, ends_at, status, confidence,
                          attrs, created_at, updated_at
                """,
                alloc_id,
                ns_id,
                res_id,
                demand_kind,
                demand_id,
                functional_location_id,
                starts_at,
                ends_at,
                status,
                confidence,
                attrs_json,
            )
        except Exception as exc:
            # Check for PostgreSQL exclusion constraint violation (23P01) or concurrency conflict
            exc_str = str(exc)
            sqlstate = getattr(exc, "sqlstate", "")
            if (
                sqlstate == "23P01"
                or "exclude_resource_double_booking" in exc_str
                or "exclusion constraint" in exc_str
                or (asyncpg and isinstance(exc, asyncpg.exceptions.ExclusionViolationError))
            ):
                raise ResourceConcurrencyError(
                    f"Resource {res_id} is already allocated during the requested window [{starts_at.isoformat()}, {ends_at.isoformat()}]."
                ) from exc
            raise

    record = {
        "id": str(row["id"]),
        "namespace_id": str(row["namespace_id"]),
        "resource_id": str(row["resource_id"]),
        "demand_kind": row["demand_kind"],
        "demand_id": str(row["demand_id"]) if row["demand_id"] else None,
        "functional_location_id": str(row["functional_location_id"])
        if row["functional_location_id"]
        else None,
        "starts_at": row["starts_at"].isoformat()
        if hasattr(row["starts_at"], "isoformat")
        else str(row["starts_at"]),
        "ends_at": row["ends_at"].isoformat()
        if hasattr(row["ends_at"], "isoformat")
        else str(row["ends_at"]),
        "status": row["status"],
        "confidence": row["confidence"],
        "attrs": json.loads(row["attrs"]) if isinstance(row["attrs"], str) else row["attrs"],
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else str(row["created_at"]),
        "updated_at": row["updated_at"].isoformat()
        if hasattr(row["updated_at"], "isoformat")
        else str(row["updated_at"]),
    }

    if params.get("contractor_view") or res_row["kind"] == "contractor":
        return redact_contractor_view(record)
    return record


async def do_release(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Release an existing allocation, freeing the resource window."""
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    alloc_id = _parse_uuid(params.get("allocation_id"), "allocation_id")

    pool = _extract_pool(engine)
    async with scoped_pg_session(pool, ns_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE allocations
            SET status = 'released', updated_at = now()
            WHERE id = $1 AND namespace_id = $2
            RETURNING id, namespace_id, resource_id, demand_kind, demand_id,
                      functional_location_id, starts_at, ends_at, status, confidence,
                      attrs, created_at, updated_at
            """,
            alloc_id,
            ns_id,
        )

    if not row:
        raise ResourceNotFoundError(f"Allocation {alloc_id} not found in namespace {ns_id}.")

    return {
        "id": str(row["id"]),
        "namespace_id": str(row["namespace_id"]),
        "resource_id": str(row["resource_id"]),
        "demand_kind": row["demand_kind"],
        "demand_id": str(row["demand_id"]) if row["demand_id"] else None,
        "functional_location_id": str(row["functional_location_id"])
        if row["functional_location_id"]
        else None,
        "starts_at": row["starts_at"].isoformat()
        if hasattr(row["starts_at"], "isoformat")
        else str(row["starts_at"]),
        "ends_at": row["ends_at"].isoformat()
        if hasattr(row["ends_at"], "isoformat")
        else str(row["ends_at"]),
        "status": row["status"],
        "confidence": row["confidence"],
        "attrs": json.loads(row["attrs"]) if isinstance(row["attrs"], str) else row["attrs"],
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else str(row["created_at"]),
        "updated_at": row["updated_at"].isoformat()
        if hasattr(row["updated_at"], "isoformat")
        else str(row["updated_at"]),
    }


async def do_detect_conflicts(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Detect overlapping active allocations for a given resource or across all resources in a window.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    res_id_raw = params.get("resource_id")
    res_id = _parse_uuid(res_id_raw, "resource_id") if res_id_raw else None

    starts_at_raw = params.get("starts_at")
    starts_at = _parse_datetime(starts_at_raw, "starts_at") if starts_at_raw else None

    ends_at_raw = params.get("ends_at")
    ends_at = _parse_datetime(ends_at_raw, "ends_at") if ends_at_raw else None

    pool = _extract_pool(engine)
    async with scoped_pg_session(pool, ns_id) as conn:
        query = """
            SELECT a1.id AS alloc_id_1, a1.resource_id, a1.starts_at AS starts_at_1, a1.ends_at AS ends_at_1, a1.demand_kind AS demand_kind_1,
                   a2.id AS alloc_id_2, a2.starts_at AS starts_at_2, a2.ends_at AS ends_at_2, a2.demand_kind AS demand_kind_2
            FROM allocations a1
            JOIN allocations a2 ON a1.resource_id = a2.resource_id
                               AND a1.id < a2.id
                               AND tstzrange(a1.starts_at, a1.ends_at) && tstzrange(a2.starts_at, a2.ends_at)
                               AND a1.status <> 'released'
                               AND a2.status <> 'released'
            WHERE a1.namespace_id = $1 AND a2.namespace_id = $1
        """
        args: list[Any] = [ns_id]
        if res_id:
            query += f" AND a1.resource_id = ${len(args) + 1}"
            args.append(res_id)
        if starts_at and ends_at:
            query += f" AND tstzrange(a1.starts_at, a1.ends_at) && tstzrange(${len(args) + 1}, ${len(args) + 2})"
            args.extend([starts_at, ends_at])

        rows = await conn.fetch(query, *args)

    conflicts = []
    for r in rows:
        conflicts.append(
            {
                "resource_id": str(r["resource_id"]),
                "allocation_1": {
                    "id": str(r["alloc_id_1"]),
                    "starts_at": r["starts_at_1"].isoformat()
                    if hasattr(r["starts_at_1"], "isoformat")
                    else str(r["starts_at_1"]),
                    "ends_at": r["ends_at_1"].isoformat()
                    if hasattr(r["ends_at_1"], "isoformat")
                    else str(r["ends_at_1"]),
                    "demand_kind": r["demand_kind_1"],
                },
                "allocation_2": {
                    "id": str(r["alloc_id_2"]),
                    "starts_at": r["starts_at_2"].isoformat()
                    if hasattr(r["starts_at_2"], "isoformat")
                    else str(r["starts_at_2"]),
                    "ends_at": r["ends_at_2"].isoformat()
                    if hasattr(r["ends_at_2"], "isoformat")
                    else str(r["ends_at_2"]),
                    "demand_kind": r["demand_kind_2"],
                },
            }
        )

    return {
        "namespace_id": str(ns_id),
        "conflicts": conflicts,
        "total_conflicts": len(conflicts),
    }
