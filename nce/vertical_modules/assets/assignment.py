"""
nce/vertical_modules/assets/assignment.py
=========================================
Wave D-2: Person assignment and sub-components for Module 9 (Assets Engine):
  - do_assign_asset_person: links EMPLOYEE:{id} -[uses]-> ASSET:{id} in kg_edges
    with C16 principal resolution support (principal_id -> employee_id).
  - do_unassign_asset_person: removes the uses edge for an asset.
  - do_get_person_assets: lists all assets assigned to an employee, with optional
    open service ticket fault integration (replaces portal person-assets / person-utstyr-feil).
  - do_link_subcomponent: links ASSET:{sub_id} -[part_of]-> ASSET:{parent_id} in
    kg_edges (replaces portal utstyr/{id}/sfp) with cycle and self-link checks.
  - do_unlink_subcomponent: unlinks a sub-component from its parent asset.
  - do_get_asset_subcomponents: retrieves child sub-components and parent asset hierarchy.

Strict Tenant Predicate Discipline (Charter §5.5)
-------------------------------------------------
Every query against tenant tables (assets, principal_bindings, service_tickets,
kg_edges) enforces explicit WHERE namespace_id = $N::uuid.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.mcp_errors import BusinessRefusalError

log = logging.getLogger("nce.vertical_modules.assets.assignment")

_ASSETS_ENGINE: str = "assets"
_NODE_TYPE_ASSET: str = "ASSET"


class AssetAssignmentError(BusinessRefusalError):
    """Base exception for asset assignment operations."""


class AssetNotFoundError(AssetAssignmentError, ValueError):
    """Raised when an asset does not exist in the tenant namespace."""

    def __init__(self, asset_id: str, message: str | None = None) -> None:
        self.asset_id = asset_id
        super().__init__(message or f"Asset not found: {asset_id}")


class PersonNotFoundError(AssetAssignmentError, ValueError):
    """Raised when an employee or person cannot be found."""

    def __init__(self, person_id: str, message: str | None = None) -> None:
        self.person_id = person_id
        super().__init__(message or f"Person / employee not found: {person_id}")


class PrincipalBindingNotFoundError(PersonNotFoundError):
    """Raised when an employee or principal binding cannot be resolved."""

    def __init__(self, principal_id: str, message: str | None = None) -> None:
        self.principal_id = principal_id
        super().__init__(
            person_id=principal_id,
            message=message or f"No principal binding found for: {principal_id}",
        )


class SubcomponentCycleError(AssetAssignmentError, ValueError):
    """Raised when attempting a self-referential or circular sub-component link."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _extract_pool(engine_or_pool: Any) -> Any:
    """Extract an asyncpg pool or pool-like object from engine or pool."""
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str) -> UUID:
    """Validate and parse a UUID argument."""
    if isinstance(val, UUID):
        return val
    if not val:
        raise ValueError(f"{field_name} is required")
    try:
        return UUID(str(val).strip())
    except Exception as exc:
        raise ValueError(f"{field_name} must be a valid UUID: {val}") from exc


def _row_to_asset_dict(row: Any) -> dict[str, Any]:
    """Convert an assets table Record to a JSON-safe dictionary."""
    if isinstance(row, dict):
        d = row
    else:
        try:
            d = dict(row)
        except Exception:
            d = {}

    created_at = d.get("created_at")
    updated_at = d.get("updated_at")

    return {
        "id": str(d.get("id", "")),
        "bom_line_id": d.get("bom_line_id"),
        "serial": d.get("serial"),
        "functional_location_id": d.get("functional_location_id"),
        "lifecycle_state": d.get("lifecycle_state"),
        "is_shell": bool(d.get("is_shell", False)),
        "product_id": str(d["product_id"]) if d.get("product_id") else None,
        "product_sku": d.get("product_sku"),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else None,
    }


# ===========================================================================
# 1. Person Assignment Operations
# ===========================================================================


async def do_assign_asset_person(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Assign an asset to a person / employee (EMPLOYEE -[uses]-> ASSET).

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - asset_id: (required) asset UUID string or UUID.
        - employee_id: (optional) employee identifier (e.g. email or employee UUID/ID).
        - principal_id: (optional) caller principal_id, resolved via C16 principal_bindings.
        - role: (optional) assignment role label (defaults to 'user').
        - change_origin: (optional) origin enum (defaults to 'agent').

    Returns
    -------
    dict with:
        - ok: True
        - asset_id: str
        - employee_id: str
        - predicate: 'uses'
        - status: 'assigned'
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    asset_uuid = _parse_uuid(params.get("asset_id"), "asset_id")

    employee_id = str(params.get("employee_id") or "").strip()
    principal_id = str(params.get("principal_id") or "").strip()
    change_origin = str(params.get("change_origin") or "agent").strip()

    if not employee_id and not principal_id:
        raise ValueError("Either 'employee_id' or 'principal_id' must be provided")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Resolve principal_id -> employee_id if employee_id not directly provided
        if not employee_id:
            binding_row = await conn.fetchrow(
                """
                SELECT employee_id
                FROM principal_bindings
                WHERE namespace_id = $1::uuid AND principal_id = $2
                """,
                ns_uuid,
                principal_id,
            )
            if binding_row is None or not binding_row["employee_id"]:
                raise PrincipalBindingNotFoundError(
                    principal_id=principal_id,
                    message=f"No principal binding found for: {principal_id}",
                )
            employee_id = str(binding_row["employee_id"]).strip()

        # 2. Verify asset exists in tenant namespace
        asset_row = await conn.fetchrow(
            """
            SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state
            FROM assets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            asset_uuid,
            ns_uuid,
        )
        if asset_row is None:
            raise AssetNotFoundError(asset_id=str(asset_uuid))

        # 3. Contract-A Ownership Guard
        await assert_owner(conn, ns_uuid, _NODE_TYPE_ASSET, _ASSETS_ENGINE)

        # 4. Upsert EMPLOYEE:{employee_id} -[uses]-> ASSET:{asset_id} edge
        subject_label = f"EMPLOYEE:{employee_id}"
        object_label = f"ASSET:{asset_uuid}"

        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, 'uses', $2, 1.0, $3::uuid, $4
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id)
            DO UPDATE SET confidence = EXCLUDED.confidence,
                          change_origin = EXCLUDED.change_origin,
                          updated_at = NOW()
            """,
            subject_label,
            object_label,
            ns_uuid,
            change_origin,
        )

        # 5. Touch asset updated_at
        now = datetime.datetime.now(datetime.timezone.utc)
        await conn.execute(
            """
            UPDATE assets
            SET updated_at = $1::timestamptz
            WHERE id = $2::uuid AND namespace_id = $3::uuid
            """,
            now,
            asset_uuid,
            ns_uuid,
        )

    return {
        "ok": True,
        "asset_id": str(asset_uuid),
        "employee_id": employee_id,
        "predicate": "uses",
        "status": "assigned",
    }


async def do_unassign_asset_person(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Unassign an asset from a person / employee.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - asset_id: (required) asset UUID string or UUID.
        - employee_id: (optional) if supplied, only unassigns this specific employee;
          if omitted, removes any active uses edge for this asset.

    Returns
    -------
    dict with:
        - ok: True
        - asset_id: str
        - unassigned: True
        - employee_id: str | None
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    asset_uuid = _parse_uuid(params.get("asset_id"), "asset_id")
    employee_id_raw = params.get("employee_id")
    employee_id = str(employee_id_raw).strip() if employee_id_raw else None

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # Verify asset exists
        asset_row = await conn.fetchrow(
            """
            SELECT id FROM assets WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            asset_uuid,
            ns_uuid,
        )
        if asset_row is None:
            raise AssetNotFoundError(asset_id=str(asset_uuid))

        # Assert ownership
        await assert_owner(conn, ns_uuid, _NODE_TYPE_ASSET, _ASSETS_ENGINE)

        object_label = f"ASSET:{asset_uuid}"
        if employee_id:
            subject_label = f"EMPLOYEE:{employee_id}"
            await conn.execute(
                """
                DELETE FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND subject_label = $2
                  AND predicate = 'uses'
                  AND object_label = $3
                """,
                ns_uuid,
                subject_label,
                object_label,
            )
        else:
            await conn.execute(
                """
                DELETE FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND predicate = 'uses'
                  AND object_label = $2
                """,
                ns_uuid,
                object_label,
            )

        now = datetime.datetime.now(datetime.timezone.utc)
        await conn.execute(
            """
            UPDATE assets
            SET updated_at = $1::timestamptz
            WHERE id = $2::uuid AND namespace_id = $3::uuid
            """,
            now,
            asset_uuid,
            ns_uuid,
        )

    return {
        "ok": True,
        "asset_id": str(asset_uuid),
        "unassigned": True,
        "employee_id": employee_id,
    }


async def do_get_person_assets(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve all assets assigned to an employee, with optional fault report.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - employee_id: (optional) employee identifier.
        - principal_id: (optional) caller principal_id, resolved via C16 principal_bindings.
        - include_faults: (optional bool) whether to join active service tickets.

    Returns
    -------
    dict with:
        - ok: True
        - employee_id: str
        - assets: list[dict]
        - faults: list[dict] (if include_faults is True, else omitted)
        - count: int
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = str(params.get("employee_id") or "").strip()
    principal_id = str(params.get("principal_id") or "").strip()
    include_faults = bool(params.get("include_faults", False))

    if not employee_id and not principal_id:
        raise ValueError("Either 'employee_id' or 'principal_id' must be provided")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # Resolve principal_id -> employee_id if needed
        if not employee_id:
            binding_row = await conn.fetchrow(
                """
                SELECT employee_id
                FROM principal_bindings
                WHERE namespace_id = $1::uuid AND principal_id = $2
                """,
                ns_uuid,
                principal_id,
            )
            if binding_row is None or not binding_row["employee_id"]:
                raise PrincipalBindingNotFoundError(
                    principal_id=principal_id,
                    message=f"No principal binding found for: {principal_id}",
                )
            employee_id = str(binding_row["employee_id"]).strip()

        subject_label = f"EMPLOYEE:{employee_id}"
        edge_rows = await conn.fetch(
            """
            SELECT object_label, confidence, change_origin, created_at, updated_at
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND subject_label = $2
              AND predicate = 'uses'
            ORDER BY created_at ASC
            """,
            ns_uuid,
            subject_label,
        )

        asset_uuids: list[UUID] = []
        for edge in edge_rows:
            obj = edge["object_label"]
            if obj.upper().startswith("ASSET:"):
                raw_id = obj.split(":", 1)[1]
                try:
                    asset_uuids.append(UUID(raw_id))
                except Exception:
                    continue

        assets: list[dict[str, Any]] = []
        faults: list[dict[str, Any]] = []

        if asset_uuids:
            asset_rows = await conn.fetch(
                """
                SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state,
                       is_shell, product_id, product_sku, created_at, updated_at
                FROM assets
                WHERE namespace_id = $1::uuid AND id = ANY($2::uuid[])
                ORDER BY created_at ASC
                """,
                ns_uuid,
                asset_uuids,
            )
            for r in asset_rows:
                assets.append(_row_to_asset_dict(r))

            if include_faults:
                # Query open/active tickets for any of these assets
                ticket_rows = await conn.fetch(
                    """
                    SELECT id, status, priority, summary, description, asset_id, room_id, created_at
                    FROM service_tickets
                    WHERE namespace_id = $1::uuid
                      AND asset_id = ANY($2::uuid[])
                      AND status NOT IN ('resolved', 'closed', 'cancelled')
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    ns_uuid,
                    asset_uuids,
                )
                for tr in ticket_rows:
                    faults.append(
                        {
                            "ticket_id": str(tr["id"]),
                            "asset_id": str(tr["asset_id"]) if tr["asset_id"] else None,
                            "status": tr["status"],
                            "priority": tr["priority"],
                            "summary": tr["summary"],
                            "description": tr["description"],
                            "room_id": tr["room_id"],
                            "created_at": tr["created_at"].isoformat()
                            if tr["created_at"]
                            else None,
                        }
                    )

        result: dict[str, Any] = {
            "ok": True,
            "employee_id": employee_id,
            "assets": assets,
            "count": len(assets),
        }
        if include_faults:
            result["faults"] = faults
            result["fault_count"] = len(faults)

        return result


# ===========================================================================
# 2. Sub-components Hierarchy Operations
# ===========================================================================


async def do_link_subcomponent(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Link an asset as a sub-component of a parent asset (ASSET -[part_of]-> ASSET).

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - parent_asset_id: (required) parent asset UUID string or UUID.
        - sub_asset_id: (required) sub-component asset UUID string or UUID.
        - relation: (optional) predicate label (defaults to 'part_of').
        - change_origin: (optional) origin enum (defaults to 'agent').

    Returns
    -------
    dict with:
        - ok: True
        - parent_asset_id: str
        - sub_asset_id: str
        - predicate: 'part_of'
        - status: 'linked'
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    parent_uuid = _parse_uuid(
        params.get("parent_asset_id") or params.get("asset_id"), "parent_asset_id"
    )
    sub_uuid = _parse_uuid(
        params.get("sub_asset_id") or params.get("child_asset_id"), "sub_asset_id"
    )

    relation = str(params.get("relation") or "part_of").strip()
    change_origin = str(params.get("change_origin") or "agent").strip()

    if parent_uuid == sub_uuid:
        raise SubcomponentCycleError("An asset cannot be linked as a sub-component of itself")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Verify parent asset exists
        parent_row = await conn.fetchrow(
            """
            SELECT id FROM assets WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            parent_uuid,
            ns_uuid,
        )
        if parent_row is None:
            raise AssetNotFoundError(
                asset_id=str(parent_uuid), message=f"Parent asset not found: {parent_uuid}"
            )

        # 2. Verify sub asset exists
        sub_row = await conn.fetchrow(
            """
            SELECT id FROM assets WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            sub_uuid,
            ns_uuid,
        )
        if sub_row is None:
            raise AssetNotFoundError(
                asset_id=str(sub_uuid), message=f"Sub-component asset not found: {sub_uuid}"
            )

        # 3. Guard against circular hierarchy: check if sub_uuid is already an ancestor of parent_uuid
        cycle_check = await conn.fetchval(
            """
            WITH RECURSIVE ancestors AS (
                SELECT object_label
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND subject_label = $2
                  AND predicate = 'part_of'
                UNION
                SELECT e.object_label
                FROM kg_edges e
                JOIN ancestors a ON e.subject_label = a.object_label
                WHERE e.namespace_id = $1::uuid
                  AND e.predicate = 'part_of'
            )
            SELECT 1 FROM ancestors WHERE object_label = $3 LIMIT 1
            """,
            ns_uuid,
            f"ASSET:{parent_uuid}",
            f"ASSET:{sub_uuid}",
        )
        if cycle_check is not None:
            raise SubcomponentCycleError(
                f"Circular dependency detected: linking {sub_uuid} as part_of {parent_uuid} creates a cycle"
            )

        # 4. Assert ownership
        await assert_owner(conn, ns_uuid, _NODE_TYPE_ASSET, _ASSETS_ENGINE)

        # 5. Insert / upsert ASSET:{sub_id} -[part_of]-> ASSET:{parent_id}
        subject_label = f"ASSET:{sub_uuid}"
        object_label = f"ASSET:{parent_uuid}"

        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, $2, $3, 1.0, $4::uuid, $5
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id)
            DO UPDATE SET confidence = EXCLUDED.confidence,
                          change_origin = EXCLUDED.change_origin,
                          updated_at = NOW()
            """,
            subject_label,
            relation,
            object_label,
            ns_uuid,
            change_origin,
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        await conn.execute(
            """
            UPDATE assets
            SET updated_at = $1::timestamptz
            WHERE id IN ($2::uuid, $3::uuid) AND namespace_id = $4::uuid
            """,
            now,
            parent_uuid,
            sub_uuid,
            ns_uuid,
        )

    return {
        "ok": True,
        "parent_asset_id": str(parent_uuid),
        "sub_asset_id": str(sub_uuid),
        "predicate": relation,
        "status": "linked",
    }


async def do_unlink_subcomponent(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Unlink a sub-component from its parent asset.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - parent_asset_id: (required) parent asset UUID string or UUID.
        - sub_asset_id: (required) sub-component asset UUID string or UUID.

    Returns
    -------
    dict with:
        - ok: True
        - parent_asset_id: str
        - sub_asset_id: str
        - unlinked: True
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    parent_uuid = _parse_uuid(
        params.get("parent_asset_id") or params.get("asset_id"), "parent_asset_id"
    )
    sub_uuid = _parse_uuid(
        params.get("sub_asset_id") or params.get("child_asset_id"), "sub_asset_id"
    )

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # Assert ownership
        await assert_owner(conn, ns_uuid, _NODE_TYPE_ASSET, _ASSETS_ENGINE)

        subject_label = f"ASSET:{sub_uuid}"
        object_label = f"ASSET:{parent_uuid}"

        await conn.execute(
            """
            DELETE FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND subject_label = $2
              AND predicate = 'part_of'
              AND object_label = $3
            """,
            ns_uuid,
            subject_label,
            object_label,
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        await conn.execute(
            """
            UPDATE assets
            SET updated_at = $1::timestamptz
            WHERE id IN ($2::uuid, $3::uuid) AND namespace_id = $4::uuid
            """,
            now,
            parent_uuid,
            sub_uuid,
            ns_uuid,
        )

    return {
        "ok": True,
        "parent_asset_id": str(parent_uuid),
        "sub_asset_id": str(sub_uuid),
        "unlinked": True,
    }


async def do_get_asset_subcomponents(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve sub-components (children) and parent asset for a given asset.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - asset_id: (required) asset UUID string or UUID.

    Returns
    -------
    dict with:
        - ok: True
        - asset_id: str
        - parent_asset: dict | None
        - sub_components: list[dict]
        - count: int (number of sub-components)
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    asset_uuid = _parse_uuid(params.get("asset_id"), "asset_id")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Verify target asset exists
        target_row = await conn.fetchrow(
            """
            SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state,
                   is_shell, product_id, product_sku, created_at, updated_at
            FROM assets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            asset_uuid,
            ns_uuid,
        )
        if target_row is None:
            raise AssetNotFoundError(asset_id=str(asset_uuid))

        # 2. Query child sub-components (ASSET:* -[part_of]-> ASSET:{asset_id})
        child_edges = await conn.fetch(
            """
            SELECT subject_label, predicate, confidence, change_origin, created_at
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND object_label = $2
              AND predicate = 'part_of'
            ORDER BY created_at ASC
            """,
            ns_uuid,
            f"ASSET:{asset_uuid}",
        )

        child_uuids: list[UUID] = []
        for edge in child_edges:
            subj = edge["subject_label"]
            if subj.upper().startswith("ASSET:"):
                raw_id = subj.split(":", 1)[1]
                try:
                    child_uuids.append(UUID(raw_id))
                except Exception:
                    continue

        sub_components: list[dict[str, Any]] = []
        if child_uuids:
            child_rows = await conn.fetch(
                """
                SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state,
                       is_shell, product_id, product_sku, created_at, updated_at
                FROM assets
                WHERE namespace_id = $1::uuid AND id = ANY($2::uuid[])
                ORDER BY created_at ASC
                """,
                ns_uuid,
                child_uuids,
            )
            for cr in child_rows:
                sub_components.append(_row_to_asset_dict(cr))

        # 3. Query parent asset (ASSET:{asset_id} -[part_of]-> ASSET:*)
        parent_edge = await conn.fetchrow(
            """
            SELECT object_label
            FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND subject_label = $2
              AND predicate = 'part_of'
            LIMIT 1
            """,
            ns_uuid,
            f"ASSET:{asset_uuid}",
        )

        parent_asset: dict[str, Any] | None = None
        if (
            parent_edge
            and "object_label" in parent_edge
            and parent_edge["object_label"]
            and str(parent_edge["object_label"]).upper().startswith("ASSET:")
        ):
            parent_raw = str(parent_edge["object_label"]).split(":", 1)[1]
            try:
                parent_parsed_uuid = UUID(parent_raw)
                p_row = await conn.fetchrow(
                    """
                    SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state,
                           is_shell, product_id, product_sku, created_at, updated_at
                    FROM assets
                    WHERE id = $1::uuid AND namespace_id = $2::uuid
                    """,
                    parent_parsed_uuid,
                    ns_uuid,
                )
                if p_row:
                    parent_asset = _row_to_asset_dict(p_row)
            except Exception:
                parent_asset = None

    return {
        "ok": True,
        "asset_id": str(asset_uuid),
        "parent_asset": parent_asset,
        "sub_components": sub_components,
        "count": len(sub_components),
    }
