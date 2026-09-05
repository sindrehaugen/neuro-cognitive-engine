"""
nce.vertical_modules.resources.registry
========================================
Resource master data registry for Module 15 (Staff & Resources Engine).
Provides a unified schedulable abstraction over employees, contractors, vehicles, and tools.

RS-2 Hardening:
A van is a VEHICLE (owned and scheduled here). It is also a STOCK_LOCATION in Inventory,
and NEVER a customer FUNCTIONAL_LOCATION.

Functions:
  - do_create_resource: Register a schedulable resource.
  - do_get_resource: Retrieve a resource by ID.
  - do_list_resources: List resources with filtering and pagination.
  - do_update_resource: Update mutable attributes.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceNotFoundError,
    ResourceValidationError,
    require_resources_enabled,
)

log = logging.getLogger("nce.vertical_modules.resources.registry")

VALID_RESOURCE_KINDS: frozenset[str] = frozenset({"employee", "contractor", "vehicle", "tool"})


def _extract_pool(engine_or_pool: Any) -> Any:
    if isinstance(engine_or_pool, dict) and "pg_pool" in engine_or_pool:
        return engine_or_pool["pg_pool"]
    if hasattr(engine_or_pool, "pg_pool"):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, name: str) -> UUID:
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val).strip())
    except (ValueError, AttributeError) as exc:
        raise ResourceValidationError(f"Invalid UUID for {name}: {val!r}") from exc


async def do_create_resource(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create and register a new schedulable resource.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - kind: (required) One of 'employee', 'contractor', 'vehicle', 'tool'.
        - display_name: (required) Human-readable name.
        - ref_id: (optional) Upstream identity (e.g. employee_id, contractor_id).
        - attrs: (optional) Arbitrary metadata attributes.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    kind = str(params.get("kind", "")).strip().lower()
    if kind in ("functional_location", "site", "room", "rack"):
        raise ResourceValidationError(
            "RS-2 Violation: A van is a VEHICLE, never a customer FUNCTIONAL_LOCATION."
        )
    if kind not in VALID_RESOURCE_KINDS:
        raise ResourceValidationError(
            f"Invalid resource kind: {kind!r}. Must be one of {sorted(VALID_RESOURCE_KINDS)}."
        )

    display_name = str(params.get("display_name", "")).strip()
    if not display_name:
        raise ResourceValidationError("display_name is required and cannot be blank.")

    ref_id = params.get("ref_id")
    if ref_id is not None:
        ref_id = str(ref_id).strip()

    attrs = params.get("attrs")
    if attrs is None:
        attrs = {}
    elif not isinstance(attrs, dict):
        raise ResourceValidationError("attrs must be a dictionary.")

    pool = _extract_pool(engine)
    res_id = uuid4()
    attrs_json = json.dumps(attrs)

    async with scoped_pg_session(pool, ns_id) as conn:
        await conn.execute(
            """
            INSERT INTO resources (id, namespace_id, kind, ref_id, display_name, attrs)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            res_id,
            ns_id,
            kind,
            ref_id,
            display_name,
            attrs_json,
        )

    return {
        "id": str(res_id),
        "namespace_id": str(ns_id),
        "kind": kind,
        "ref_id": ref_id,
        "display_name": display_name,
        "attrs": attrs,
    }


async def do_get_resource(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve a resource by ID within the tenant scope."""
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    res_id = _parse_uuid(params.get("resource_id"), "resource_id")

    pool = _extract_pool(engine)
    async with scoped_pg_session(pool, ns_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, namespace_id, kind, ref_id, display_name, attrs, created_at, updated_at
            FROM resources
            WHERE id = $1 AND namespace_id = $2
            """,
            res_id,
            ns_id,
        )

    if not row:
        raise ResourceNotFoundError(f"Resource {res_id} not found in namespace {ns_id}.")

    attrs = row["attrs"]
    if isinstance(attrs, str):
        attrs = json.loads(attrs)

    return {
        "id": str(row["id"]),
        "namespace_id": str(row["namespace_id"]),
        "kind": row["kind"],
        "ref_id": row["ref_id"],
        "display_name": row["display_name"],
        "attrs": attrs,
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else str(row["created_at"]),
        "updated_at": row["updated_at"].isoformat()
        if hasattr(row["updated_at"], "isoformat")
        else str(row["updated_at"]),
    }


async def do_list_resources(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List resources with optional kind filter and pagination."""
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    kind = params.get("kind")
    if kind:
        kind = str(kind).strip().lower()
        if kind not in VALID_RESOURCE_KINDS:
            raise ResourceValidationError(f"Invalid kind filter: {kind!r}")

    limit = max(1, min(int(params.get("limit", 50)), 200))
    offset = max(0, int(params.get("offset", 0)))

    pool = _extract_pool(engine)
    async with scoped_pg_session(pool, ns_id) as conn:
        if kind:
            rows = await conn.fetch(
                """
                SELECT id, namespace_id, kind, ref_id, display_name, attrs, created_at, updated_at
                FROM resources
                WHERE namespace_id = $1 AND kind = $2
                ORDER BY created_at ASC
                LIMIT $3 OFFSET $4
                """,
                ns_id,
                kind,
                limit,
                offset,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM resources WHERE namespace_id = $1 AND kind = $2",
                ns_id,
                kind,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, namespace_id, kind, ref_id, display_name, attrs, created_at, updated_at
                FROM resources
                WHERE namespace_id = $1
                ORDER BY created_at ASC
                LIMIT $2 OFFSET $3
                """,
                ns_id,
                limit,
                offset,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM resources WHERE namespace_id = $1",
                ns_id,
            )

    resources: list[dict[str, Any]] = []
    for r in rows:
        attrs = r["attrs"]
        if isinstance(attrs, str):
            attrs = json.loads(attrs)
        resources.append(
            {
                "id": str(r["id"]),
                "namespace_id": str(r["namespace_id"]),
                "kind": r["kind"],
                "ref_id": r["ref_id"],
                "display_name": r["display_name"],
                "attrs": attrs,
                "created_at": r["created_at"].isoformat()
                if hasattr(r["created_at"], "isoformat")
                else str(r["created_at"]),
                "updated_at": r["updated_at"].isoformat()
                if hasattr(r["updated_at"], "isoformat")
                else str(r["updated_at"]),
            }
        )

    return {
        "resources": resources,
        "total": total if total is not None else len(resources),
        "limit": limit,
        "offset": offset,
    }


async def do_update_resource(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Update a resource's display_name or attrs within the tenant scope."""
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    res_id = _parse_uuid(params.get("resource_id"), "resource_id")

    display_name = params.get("display_name")
    if display_name is not None:
        display_name = str(display_name).strip()
        if not display_name:
            raise ResourceValidationError("display_name cannot be blank.")

    attrs = params.get("attrs")
    if attrs is not None and not isinstance(attrs, dict):
        raise ResourceValidationError("attrs must be a dictionary.")

    pool = _extract_pool(engine)
    async with scoped_pg_session(pool, ns_id) as conn:
        existing = await conn.fetchrow(
            "SELECT attrs, display_name FROM resources WHERE id = $1 AND namespace_id = $2",
            res_id,
            ns_id,
        )
        if not existing:
            raise ResourceNotFoundError(f"Resource {res_id} not found in namespace {ns_id}.")

        cur_attrs = existing["attrs"]
        if isinstance(cur_attrs, str):
            cur_attrs = json.loads(cur_attrs)
        elif not isinstance(cur_attrs, dict):
            cur_attrs = {}

        if attrs:
            cur_attrs.update(attrs)

        new_name = display_name if display_name is not None else existing["display_name"]

        await conn.execute(
            """
            UPDATE resources
            SET display_name = $3, attrs = $4::jsonb, updated_at = now()
            WHERE id = $1 AND namespace_id = $2
            """,
            res_id,
            ns_id,
            new_name,
            json.dumps(cur_attrs),
        )

    return await do_get_resource(engine, {"namespace_id": ns_id, "resource_id": res_id})
