"""nce.vertical_modules.system_design.design_versions — DESIGN Versions & Room Specifications.

Phase C Wave C-3:
Manages solution design versions associated with functional locations,
active-design version toggling (POST /{id}/set-active), and structured
ROOM_SPEC metadata (dimensions, acoustics, display, seating).

Architecture & Governance (Charter §13 & §14 K-C3):
- Backed by kg_nodes (entity_type='DESIGN'), kg_edges (_PRED_CONTAINS),
  and system_design_geometry version/meta rows.
- Atomic active-version toggle: exactly one design version is active per FL.
- ROOM_SPEC stored as design metadata document.
- Fully supports asyncpg connection and in-memory mock store for unit testing.
- Strict MCP error hierarchy inheriting from ValueError and KeyError.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.system_design.design_versions")

_NODE_TYPE_DESIGN = "DESIGN"
_NODE_TYPE_FL = "FUNCTIONAL_LOCATION"
_SYSTEM_DESIGN_ENGINE = "system_design"
_PRED_CONTAINS = "contains"
_PRED_HAS_ACTIVE_DESIGN = "has_active_design"
_STRUCTURAL_CONFIDENCE = 1.0


# ---------------------------------------------------------------------------
# Exceptions (Strict MCP Error Hierarchy)
# ---------------------------------------------------------------------------


class DesignVersionError(ValueError):
    """Base exception for design version operations."""


class DesignNotFoundError(DesignVersionError, KeyError):
    """Raised when a specified design does not exist."""


class InvalidDesignError(DesignVersionError, ValueError):
    """Raised when design input parameters are malformed or invalid."""


class InvalidRoomSpecError(DesignVersionError, ValueError):
    """Raised when room specification metadata is malformed or invalid."""


# ---------------------------------------------------------------------------
# In-memory mock store for unit testing (when conn is None)
# ---------------------------------------------------------------------------

_MEM_DESIGNS: dict[str, dict[str, dict[str, Any]]] = {}


def _clear_mem_store() -> None:
    """Clear in-memory mock store (for test cleanup)."""
    _MEM_DESIGNS.clear()


# ---------------------------------------------------------------------------
# Canonical Label Helpers
# ---------------------------------------------------------------------------


def canonical_design_label(design_id: str) -> str:
    """Canonical DESIGN label: ``DESIGN:<DESIGN_ID>`` (upper-cased)."""
    clean_id = design_id.strip()
    if clean_id.upper().startswith("DESIGN:"):
        return clean_id.upper()
    return f"DESIGN:{clean_id.upper()}"


def normalize_fl_identifier(fl_id: str) -> str:
    """Normalize a functional location ID or label."""
    clean_id = fl_id.strip()
    if clean_id.upper().startswith("FL:"):
        return clean_id.upper()
    return f"FL:{clean_id.upper()}"


def clean_design_id(design_id: str) -> str:
    """Extract raw identifier from a DESIGN label or raw ID."""
    clean = design_id.strip()
    if clean.upper().startswith("DESIGN:"):
        return clean[7:]
    return clean


# ---------------------------------------------------------------------------
# ROOM_SPEC Schema & Validation
# ---------------------------------------------------------------------------


def validate_room_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a ROOM_SPEC dictionary.

    Keys:
      - width_m (float >= 0)
      - depth_m (float >= 0)
      - height_m (float >= 0)
      - ceiling_height_m (float >= 0)
      - seating_capacity (int >= 0)
      - category (str)
      - target_rt60_s (float >= 0)
      - target_spl_dba (float >= 0)
      - display_type (str)
      - display_size_in (float >= 0)
      - camera_type (str)
      - mic_type (str)
      - speaker_type (str)
      - typical_features (list[str])
      - notes (str)
    """
    if not isinstance(spec, dict):
        raise InvalidRoomSpecError(f"room_spec must be a dictionary, got {type(spec).__name__}")

    cleaned: dict[str, Any] = {}
    float_keys = (
        "width_m",
        "depth_m",
        "height_m",
        "ceiling_height_m",
        "target_rt60_s",
        "target_spl_dba",
        "display_size_in",
    )
    for k in float_keys:
        if k in spec and spec[k] is not None:
            try:
                val = float(spec[k])
                if val < 0:
                    raise ValueError(f"{k} must be non-negative")
                cleaned[k] = val
            except (ValueError, TypeError) as exc:
                raise InvalidRoomSpecError(f"Invalid numeric value for {k}: {exc}") from exc

    if "seating_capacity" in spec and spec["seating_capacity"] is not None:
        try:
            val = int(spec["seating_capacity"])
            if val < 0:
                raise ValueError("seating_capacity must be non-negative")
            cleaned["seating_capacity"] = val
        except (ValueError, TypeError) as exc:
            raise InvalidRoomSpecError(
                f"Invalid integer value for seating_capacity: {exc}"
            ) from exc

    str_keys = (
        "category",
        "display_type",
        "camera_type",
        "mic_type",
        "speaker_type",
        "notes",
    )
    for k in str_keys:
        if k in spec and spec[k] is not None:
            cleaned[k] = str(spec[k]).strip()

    if "typical_features" in spec and spec["typical_features"] is not None:
        feats = spec["typical_features"]
        if not isinstance(feats, (list, tuple)):
            raise InvalidRoomSpecError("typical_features must be a list of strings")
        cleaned["typical_features"] = [str(f).strip() for f in feats if str(f).strip()]

    # Preserve any arbitrary custom keys provided in spec
    for k, v in spec.items():
        if (
            k not in cleaned
            and k not in float_keys
            and k not in str_keys
            and k != "seating_capacity"
        ):
            cleaned[k] = v

    return cleaned


# ---------------------------------------------------------------------------
# Core Domain Operations
# ---------------------------------------------------------------------------


async def create_design(
    conn: Any,
    namespace_id: UUID | str,
    design_id: str,
    name: str,
    functional_location_id: str,
    version: int = 1,
    revision: str | None = None,
    room_spec: dict[str, Any] | None = None,
    is_active: bool = False,
    metadata: dict[str, Any] | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Create a new solution design version associated with a functional location."""
    if not design_id or not str(design_id).strip():
        raise InvalidDesignError("design_id is required and cannot be empty")
    if not name or not str(name).strip():
        raise InvalidDesignError("name is required and cannot be empty")
    if not functional_location_id or not str(functional_location_id).strip():
        raise InvalidDesignError("functional_location_id is required and cannot be empty")

    raw_id = clean_design_id(design_id)
    design_lbl = canonical_design_label(raw_id)
    fl_lbl = normalize_fl_identifier(functional_location_id)
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    validated_room_spec = validate_room_spec(room_spec) if room_spec else {}
    meta_payload = {
        "name": str(name).strip(),
        "functional_location_id": fl_lbl,
        "is_active": bool(is_active),
        "revision": str(revision).strip() if revision else None,
        "room_spec": validated_room_spec,
        "metadata": metadata or {},
        "source_id": source_id,
    }

    if conn is not None:
        await assert_owner(conn, ns_uuid, _NODE_TYPE_DESIGN, _SYSTEM_DESIGN_ENGINE)

        # 1. Upsert kg_nodes DESIGN row
        await conn.execute(
            """
            INSERT INTO kg_nodes
                (label, entity_type, namespace_id, change_origin, system_design_source_id)
            VALUES ($1, $2, $3::uuid, 'sync', $4)
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET entity_type = EXCLUDED.entity_type,
                    change_origin = 'sync',
                    system_design_source_id = COALESCE(
                        EXCLUDED.system_design_source_id,
                        kg_nodes.system_design_source_id
                    ),
                    updated_at = NOW()
            """,
            design_lbl,
            _NODE_TYPE_DESIGN,
            ns_str,
            source_id,
        )

        # 2. Upsert edge DESIGN -[contains]-> FUNCTIONAL_LOCATION
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id, system_design_source_id)
            VALUES ($1, $2, $3, $4, $5::uuid, $6)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    system_design_source_id = COALESCE(
                        EXCLUDED.system_design_source_id,
                        kg_edges.system_design_source_id
                    ),
                    updated_at = NOW()
            """,
            design_lbl,
            _PRED_CONTAINS,
            fl_lbl,
            _STRUCTURAL_CONFIDENCE,
            ns_str,
            source_id,
        )

        # 3. If active, unset active on any existing designs for this FL
        if is_active:
            await conn.execute(
                """
                UPDATE system_design_geometry
                SET meta = jsonb_set(meta, '{is_active}', 'false'::jsonb),
                    updated_at = NOW()
                WHERE namespace_id = $1::uuid
                  AND node_label LIKE 'DESIGN:%'
                  AND node_label != $2
                  AND meta->>'functional_location_id' = $3
                """,
                ns_str,
                design_lbl,
                fl_lbl,
            )

        # 4. Upsert system_design_geometry design version row
        meta_json = json.dumps(meta_payload)
        await conn.execute(
            """
            INSERT INTO system_design_geometry
                (namespace_id, node_label, version, meta)
            VALUES ($1::uuid, $2, $3, $4::jsonb)
            ON CONFLICT (namespace_id, node_label) DO UPDATE
                SET version = EXCLUDED.version,
                    meta = EXCLUDED.meta,
                    updated_at = NOW()
            """,
            ns_str,
            design_lbl,
            version,
            meta_json,
        )

        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_DESIGN,
            op="upserted",
            node_id=design_lbl,
        )

        return await get_design(conn, ns_uuid, raw_id)

    # In-memory mock store
    bucket = _MEM_DESIGNS.setdefault(ns_str, {})
    if is_active:
        for other_id, other in bucket.items():
            if other.get("functional_location_id") == fl_lbl and other_id != raw_id:
                other["is_active"] = False

    record = {
        "id": raw_id,
        "label": design_lbl,
        "name": str(name).strip(),
        "functional_location_id": fl_lbl,
        "version": int(version),
        "revision": str(revision).strip() if revision else None,
        "room_spec": validated_room_spec,
        "is_active": bool(is_active),
        "metadata": metadata or {},
        "source_id": source_id,
        "created_at": "2026-09-19T12:00:00Z",
        "updated_at": "2026-09-19T12:00:00Z",
    }
    bucket[raw_id] = record
    return dict(record)


async def get_design(
    conn: Any,
    namespace_id: UUID | str,
    design_id: str,
) -> dict[str, Any]:
    """Fetch a single DESIGN by its ID or label."""
    raw_id = clean_design_id(design_id)
    design_lbl = canonical_design_label(raw_id)
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    if conn is not None:
        row = await conn.fetchrow(
            """
            SELECT node_label, version, meta, created_at, updated_at
            FROM system_design_geometry
            WHERE namespace_id = $1::uuid
              AND node_label = $2
              AND version IS NOT NULL
            """,
            ns_str,
            design_lbl,
        )
        if row is None:
            raise DesignNotFoundError(f"Design {raw_id!r} not found in namespace {ns_str}")

        meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
        created_at_iso = (
            row["created_at"].isoformat()
            if hasattr(row["created_at"], "isoformat")
            else str(row["created_at"])
        )
        updated_at_iso = (
            row["updated_at"].isoformat()
            if hasattr(row["updated_at"], "isoformat")
            else str(row["updated_at"])
        )

        return {
            "id": raw_id,
            "label": design_lbl,
            "name": meta.get("name", raw_id),
            "functional_location_id": meta.get("functional_location_id", ""),
            "version": int(row["version"]),
            "revision": meta.get("revision"),
            "room_spec": meta.get("room_spec") or {},
            "is_active": bool(meta.get("is_active", False)),
            "metadata": meta.get("metadata") or {},
            "source_id": meta.get("source_id"),
            "created_at": created_at_iso,
            "updated_at": updated_at_iso,
        }

    # In-memory mock store
    bucket = _MEM_DESIGNS.get(ns_str, {})
    if raw_id not in bucket:
        raise DesignNotFoundError(f"Design {raw_id!r} not found in namespace {ns_str}")
    return dict(bucket[raw_id])


async def list_designs(
    conn: Any,
    namespace_id: UUID | str,
    functional_location_id: str | None = None,
    is_active: bool | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List designs with optional filtering by functional location, active status, and search query."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)
    fl_lbl = normalize_fl_identifier(functional_location_id) if functional_location_id else None

    if conn is not None:
        clauses = [
            "namespace_id = $1::uuid",
            "node_label LIKE 'DESIGN:%'",
            "version IS NOT NULL",
        ]
        params: list[Any] = [ns_str]

        if fl_lbl is not None:
            params.append(fl_lbl)
            clauses.append(f"meta->>'functional_location_id' = ${len(params)}")

        if is_active is not None:
            params.append(bool(is_active))
            clauses.append(f"(meta->>'is_active')::boolean = ${len(params)}")

        if query:
            params.append(f"%{query.strip()}%")
            clauses.append(
                f"(node_label ILIKE ${len(params)} OR meta->>'name' ILIKE ${len(params)})"
            )

        params.append(int(limit))
        params.append(int(offset))
        limit_idx = len(params) - 1
        offset_idx = len(params)

        sql = f"""
            SELECT node_label, version, meta, created_at, updated_at
            FROM system_design_geometry
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """

        rows = await conn.fetch(sql, *params)
        designs: list[dict[str, Any]] = []
        for row in rows:
            lbl = row["node_label"]
            raw_id = clean_design_id(lbl)
            meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
            created_at_iso = (
                row["created_at"].isoformat()
                if hasattr(row["created_at"], "isoformat")
                else str(row["created_at"])
            )
            updated_at_iso = (
                row["updated_at"].isoformat()
                if hasattr(row["updated_at"], "isoformat")
                else str(row["updated_at"])
            )

            designs.append(
                {
                    "id": raw_id,
                    "label": lbl,
                    "name": meta.get("name", raw_id),
                    "functional_location_id": meta.get("functional_location_id", ""),
                    "version": int(row["version"]),
                    "revision": meta.get("revision"),
                    "room_spec": meta.get("room_spec") or {},
                    "is_active": bool(meta.get("is_active", False)),
                    "metadata": meta.get("metadata") or {},
                    "source_id": meta.get("source_id"),
                    "created_at": created_at_iso,
                    "updated_at": updated_at_iso,
                }
            )
        return designs

    # In-memory mock store
    bucket = _MEM_DESIGNS.get(ns_str, {})
    results: list[dict[str, Any]] = []
    for d in bucket.values():
        if fl_lbl and d.get("functional_location_id") != fl_lbl:
            continue
        if is_active is not None and d.get("is_active") != is_active:
            continue
        if query:
            q_lower = query.lower()
            if q_lower not in d.get("name", "").lower() and q_lower not in d.get("id", "").lower():
                continue
        results.append(dict(d))

    return results[offset : offset + limit]


async def update_design(
    conn: Any,
    namespace_id: UUID | str,
    design_id: str,
    name: str | None = None,
    revision: str | None = None,
    room_spec: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    """Update fields of an existing DESIGN."""
    existing = await get_design(conn, namespace_id, design_id)
    raw_id = existing["id"]
    design_lbl = existing["label"]
    fl_lbl = existing["functional_location_id"]
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    updated_name = name.strip() if name is not None and str(name).strip() else existing["name"]
    updated_revision = revision.strip() if revision is not None else existing.get("revision")
    updated_room_spec = (
        validate_room_spec(room_spec) if room_spec is not None else existing.get("room_spec", {})
    )
    updated_metadata = metadata if metadata is not None else existing.get("metadata", {})
    updated_is_active = (
        bool(is_active) if is_active is not None else existing.get("is_active", False)
    )

    meta_payload = {
        "name": updated_name,
        "functional_location_id": fl_lbl,
        "is_active": updated_is_active,
        "revision": updated_revision,
        "room_spec": updated_room_spec,
        "metadata": updated_metadata,
        "source_id": existing.get("source_id"),
    }

    if conn is not None:
        await assert_owner(conn, ns_uuid, _NODE_TYPE_DESIGN, _SYSTEM_DESIGN_ENGINE)

        if updated_is_active and not existing.get("is_active"):
            # Deactivate other designs for the same FL
            await conn.execute(
                """
                UPDATE system_design_geometry
                SET meta = jsonb_set(meta, '{is_active}', 'false'::jsonb),
                    updated_at = NOW()
                WHERE namespace_id = $1::uuid
                  AND node_label LIKE 'DESIGN:%'
                  AND node_label != $2
                  AND meta->>'functional_location_id' = $3
                """,
                ns_str,
                design_lbl,
                fl_lbl,
            )

        meta_json = json.dumps(meta_payload)
        await conn.execute(
            """
            UPDATE system_design_geometry
            SET meta = $3::jsonb,
                updated_at = NOW()
            WHERE namespace_id = $1::uuid
              AND node_label = $2
              AND version IS NOT NULL
            """,
            ns_str,
            design_lbl,
            meta_json,
        )

        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_DESIGN,
            op="updated",
            node_id=design_lbl,
        )

        return await get_design(conn, ns_uuid, raw_id)

    # In-memory mock store
    bucket = _MEM_DESIGNS.setdefault(ns_str, {})
    if updated_is_active and not existing.get("is_active"):
        for other_id, other in bucket.items():
            if other.get("functional_location_id") == fl_lbl and other_id != raw_id:
                other["is_active"] = False

    target = bucket[raw_id]
    target["name"] = updated_name
    target["revision"] = updated_revision
    target["room_spec"] = updated_room_spec
    target["metadata"] = updated_metadata
    target["is_active"] = updated_is_active
    target["updated_at"] = "2026-09-19T12:00:00Z"
    return dict(target)


async def set_active_design(
    conn: Any,
    namespace_id: UUID | str,
    design_id: str,
) -> dict[str, Any]:
    """Atomically toggle a design as active for its associated functional location.

    Sets is_active=True on this design and is_active=False on all other designs
    for the same functional location.
    """
    existing = await get_design(conn, namespace_id, design_id)
    raw_id = existing["id"]
    design_lbl = existing["label"]
    fl_lbl = existing["functional_location_id"]
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    if conn is not None:
        await assert_owner(conn, ns_uuid, _NODE_TYPE_DESIGN, _SYSTEM_DESIGN_ENGINE)

        # 1. Unset active on all sibling designs for this functional location
        await conn.execute(
            """
            UPDATE system_design_geometry
            SET meta = jsonb_set(meta, '{is_active}', 'false'::jsonb),
                updated_at = NOW()
            WHERE namespace_id = $1::uuid
              AND node_label LIKE 'DESIGN:%'
              AND meta->>'functional_location_id' = $2
            """,
            ns_str,
            fl_lbl,
        )

        # 2. Set active on this design
        await conn.execute(
            """
            UPDATE system_design_geometry
            SET meta = jsonb_set(meta, '{is_active}', 'true'::jsonb),
                updated_at = NOW()
            WHERE namespace_id = $1::uuid
              AND node_label = $2
              AND version IS NOT NULL
            """,
            ns_str,
            design_lbl,
        )

        # 3. Graph edge: FL -[has_active_design]-> DESIGN
        # First remove old active design edge from FL
        await conn.execute(
            """
            DELETE FROM kg_edges
            WHERE namespace_id = $1::uuid
              AND subject_label = $2
              AND predicate = $3
            """,
            ns_str,
            fl_lbl,
            _PRED_HAS_ACTIVE_DESIGN,
        )

        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, $2, $3, $4, $5::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    updated_at = NOW()
            """,
            fl_lbl,
            _PRED_HAS_ACTIVE_DESIGN,
            design_lbl,
            _STRUCTURAL_CONFIDENCE,
            ns_str,
        )

        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_DESIGN,
            op="updated",
            node_id=design_lbl,
        )

        return await get_design(conn, ns_uuid, raw_id)

    # In-memory mock store
    bucket = _MEM_DESIGNS.setdefault(ns_str, {})
    for other_id, other in bucket.items():
        if other.get("functional_location_id") == fl_lbl:
            other["is_active"] = other_id == raw_id

    target = bucket[raw_id]
    target["is_active"] = True
    target["updated_at"] = "2026-09-19T12:00:00Z"
    return dict(target)


async def get_active_design_for_fl(
    conn: Any,
    namespace_id: UUID | str,
    functional_location_id: str,
) -> dict[str, Any] | None:
    """Fetch the currently active design for a functional location, or None if none is active."""
    designs = await list_designs(
        conn,
        namespace_id,
        functional_location_id=functional_location_id,
        is_active=True,
        limit=1,
    )
    if designs:
        return designs[0]
    return None


async def get_room_spec(
    conn: Any,
    namespace_id: UUID | str,
    design_id: str,
) -> dict[str, Any]:
    """Read the ROOM_SPEC metadata from a design."""
    design = await get_design(conn, namespace_id, design_id)
    return design.get("room_spec") or {}


async def set_room_spec(
    conn: Any,
    namespace_id: UUID | str,
    design_id: str,
    room_spec: dict[str, Any],
) -> dict[str, Any]:
    """Set or update the ROOM_SPEC metadata on a design."""
    validated = validate_room_spec(room_spec)
    updated = await update_design(conn, namespace_id, design_id, room_spec=validated)
    return updated.get("room_spec") or {}
