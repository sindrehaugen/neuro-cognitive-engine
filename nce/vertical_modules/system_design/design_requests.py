"""
nce/vertical_modules/system_design/design_requests.py
=====================================================
Solution Design intake queue (losningsdesign-ko / DESIGN_REQUEST) management.

Charter §13 line 332 (Wave C-4) & Operations Backend §3.1 line 115 / §3.3 line 143:
- Replaces: losningsdesign-ko (3 routes in host portal)
- Backing (2026-09-20, migration 104): kg_nodes identity row
  (entity_type='DESIGN_REQUEST', label='DESIGN_REQUEST:<id>') plus real
  fields on the system_design_design_requests satellite table, joined by
  node_label -- following the exact CONTACT/097 pattern. Previously ran
  entirely on system_design_geometry's meta JSONB column keyed by
  node_label; that table is reserved for actual geometry payloads
  validated by validate_geometry()'s single choke point (same restriction
  DEVICE/RACK/CABLE and DESIGN both hit), and DESIGN_REQUEST had no
  kg_nodes row at all -- it was neither a real node type nor safely
  routable through SecondaryTable. Relationships (for_quote/targets_fl/
  assigned_to/realized_as) still live on kg_edges, unchanged.
- Manages: design request queue items with owner, status, priority, room_spec,
  and realization link to resulting DESIGN versions.
- Consumed by: from_quote.py (do_design_from_quote / fulfill_design_request_from_quote).
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.system_design.design_requests")

# Node type
_NODE_TYPE_DESIGN_REQUEST = "DESIGN_REQUEST"

# Edge predicates
_PRED_FOR_QUOTE = "for_quote"
_PRED_TARGETS_FL = "targets_fl"
_PRED_ASSIGNED_TO = "assigned_to"
_PRED_REALIZED_AS = "realized_as"

_STRUCTURAL_CONFIDENCE: float = 1.0

# Status lifecycle constants
STATUS_PENDING: str = "pending"
STATUS_ASSIGNED: str = "assigned"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"
STATUS_CANCELLED: str = "cancelled"
STATUS_REJECTED: str = "rejected"

ALLOWED_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PENDING,
        STATUS_ASSIGNED,
        STATUS_IN_PROGRESS,
        STATUS_COMPLETED,
        STATUS_CANCELLED,
        STATUS_REJECTED,
    }
)

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_CANCELLED,
        STATUS_REJECTED,
    }
)

# Priority constants
PRIORITY_LOW: str = "low"
PRIORITY_NORMAL: str = "normal"
PRIORITY_HIGH: str = "high"
PRIORITY_URGENT: str = "urgent"

ALLOWED_PRIORITIES: frozenset[str] = frozenset(
    {
        PRIORITY_LOW,
        PRIORITY_NORMAL,
        PRIORITY_HIGH,
        PRIORITY_URGENT,
    }
)

# In-memory mock store for tests without live PostgreSQL
_MEM_DESIGN_REQUESTS: dict[str, dict[str, dict[str, Any]]] = {}
_MEM_REQUEST_EDGES: dict[str, list[dict[str, Any]]] = {}


def _clear_mem_store() -> None:
    """Clear in-memory mock store (for test fixtures)."""
    _MEM_DESIGN_REQUESTS.clear()
    _MEM_REQUEST_EDGES.clear()


class DesignRequestNotFoundError(KeyError):
    """Raised when a specified DESIGN_REQUEST is not found in the given namespace."""


class InvalidDesignRequestStatusError(ValueError):
    """Raised when an invalid status or status transition is attempted."""


class InvalidDesignRequestPayloadError(ValueError):
    """Raised when payload attributes fail validation."""


def clean_request_id(request_id: str) -> str:
    """Normalize a request identifier, stripping any 'DESIGN_REQUEST:' prefix."""
    s = str(request_id).strip()
    if s.upper().startswith("DESIGN_REQUEST:"):
        return s[len("DESIGN_REQUEST:") :]
    return s


def canonical_request_label(clean_id: str) -> str:
    """Return canonical node_label: 'DESIGN_REQUEST:<clean_id>'."""
    return f"DESIGN_REQUEST:{clean_id}"


def _format_fl_label(fl_id: str | None) -> str | None:
    if not fl_id:
        return None
    fl_str = str(fl_id).strip()
    if fl_str.upper().startswith("FL:"):
        return fl_str
    return f"FL:{fl_str}"


def _format_quote_label(quote_id: str | None) -> str | None:
    if not quote_id:
        return None
    q_str = str(quote_id).strip()
    if q_str.upper().startswith("QUOTE:"):
        return q_str
    return f"QUOTE:{q_str}"


def _format_employee_label(employee_id: str | None) -> str | None:
    if not employee_id:
        return None
    emp_str = str(employee_id).strip()
    if emp_str.upper().startswith("EMPLOYEE:"):
        return emp_str
    return f"EMPLOYEE:{emp_str}"


def _format_design_label(design_id: str | None) -> str | None:
    if not design_id:
        return None
    d_str = str(design_id).strip()
    if d_str.upper().startswith("DESIGN:"):
        return d_str
    return f"DESIGN:{d_str}"


async def create_design_request(
    conn: Any,
    namespace_id: UUID | str,
    title: str,
    quote_id: str | None = None,
    functional_location_id: str | None = None,
    description: str | None = None,
    priority: str = PRIORITY_NORMAL,
    owner_id: str | None = None,
    room_spec: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Create a new solution design request in the intake queue."""
    if not title or not title.strip():
        raise InvalidDesignRequestPayloadError("Design request 'title' must not be blank.")

    p_val = (priority or PRIORITY_NORMAL).lower().strip()
    if p_val not in ALLOWED_PRIORITIES:
        raise InvalidDesignRequestPayloadError(
            f"Invalid priority {priority!r}. Must be one of {sorted(ALLOWED_PRIORITIES)}"
        )

    raw_id = clean_request_id(request_id) if request_id else str(uuid4())
    req_lbl = canonical_request_label(raw_id)
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    status = STATUS_ASSIGNED if owner_id else STATUS_PENDING
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    fl_lbl = _format_fl_label(functional_location_id)
    quote_lbl = _format_quote_label(quote_id)
    owner_lbl = _format_employee_label(owner_id)

    meta_payload = {
        "id": raw_id,
        "title": title.strip(),
        "description": description or "",
        "quote_id": quote_id,
        "functional_location_id": fl_lbl,
        "status": status,
        "priority": p_val,
        "owner_id": owner_id,
        "design_id": None,
        "room_spec": room_spec or {},
        "metadata": metadata or {},
        "created_at": now_iso,
        "updated_at": now_iso,
        "completed_at": None,
    }

    if conn is not None:
        await conn.execute(
            """
            INSERT INTO kg_nodes
                (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'operator')
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET entity_type = EXCLUDED.entity_type,
                    updated_at = NOW()
            """,
            req_lbl,
            _NODE_TYPE_DESIGN_REQUEST,
            ns_str,
        )

        await conn.execute(
            """
            INSERT INTO system_design_design_requests
                (namespace_id, node_label, title, description, quote_id,
                 functional_location_id, status, priority, owner_id,
                 design_id, room_spec, metadata)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb)
            ON CONFLICT (namespace_id, node_label) DO UPDATE
                SET title                  = EXCLUDED.title,
                    description            = EXCLUDED.description,
                    quote_id               = EXCLUDED.quote_id,
                    functional_location_id = EXCLUDED.functional_location_id,
                    status                 = EXCLUDED.status,
                    priority               = EXCLUDED.priority,
                    owner_id               = EXCLUDED.owner_id,
                    room_spec              = EXCLUDED.room_spec,
                    metadata               = EXCLUDED.metadata,
                    updated_at             = NOW()
            """,
            ns_str,
            req_lbl,
            meta_payload["title"],
            meta_payload["description"],
            meta_payload["quote_id"],
            meta_payload["functional_location_id"],
            meta_payload["status"],
            meta_payload["priority"],
            meta_payload["owner_id"],
            meta_payload["design_id"],
            json.dumps(meta_payload["room_spec"]),
            json.dumps(meta_payload["metadata"]),
        )

        if quote_lbl:
            await conn.execute(
                """
                INSERT INTO kg_edges
                    (subject_label, predicate, object_label, confidence, namespace_id)
                VALUES ($1, $2, $3, $4, $5::uuid)
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                    SET updated_at = NOW()
                """,
                req_lbl,
                _PRED_FOR_QUOTE,
                quote_lbl,
                _STRUCTURAL_CONFIDENCE,
                ns_str,
            )

        if fl_lbl:
            await conn.execute(
                """
                INSERT INTO kg_edges
                    (subject_label, predicate, object_label, confidence, namespace_id)
                VALUES ($1, $2, $3, $4, $5::uuid)
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                    SET updated_at = NOW()
                """,
                req_lbl,
                _PRED_TARGETS_FL,
                fl_lbl,
                _STRUCTURAL_CONFIDENCE,
                ns_str,
            )

        if owner_lbl:
            await conn.execute(
                """
                INSERT INTO kg_edges
                    (subject_label, predicate, object_label, confidence, namespace_id)
                VALUES ($1, $2, $3, $4, $5::uuid)
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                    SET updated_at = NOW()
                """,
                req_lbl,
                _PRED_ASSIGNED_TO,
                owner_lbl,
                _STRUCTURAL_CONFIDENCE,
                ns_str,
            )

        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_DESIGN_REQUEST,
            op="created",
            node_id=req_lbl,
        )

        return await get_design_request(conn, ns_uuid, raw_id)

    # In-memory mock store
    bucket = _MEM_DESIGN_REQUESTS.setdefault(ns_str, {})
    bucket[raw_id] = dict(meta_payload)

    edges = _MEM_REQUEST_EDGES.setdefault(ns_str, [])
    if quote_lbl:
        edges.append(
            {
                "subject_label": req_lbl,
                "predicate": _PRED_FOR_QUOTE,
                "object_label": quote_lbl,
                "confidence": _STRUCTURAL_CONFIDENCE,
                "namespace_id": ns_str,
            }
        )
    if fl_lbl:
        edges.append(
            {
                "subject_label": req_lbl,
                "predicate": _PRED_TARGETS_FL,
                "object_label": fl_lbl,
                "confidence": _STRUCTURAL_CONFIDENCE,
                "namespace_id": ns_str,
            }
        )
    if owner_lbl:
        edges.append(
            {
                "subject_label": req_lbl,
                "predicate": _PRED_ASSIGNED_TO,
                "object_label": owner_lbl,
                "confidence": _STRUCTURAL_CONFIDENCE,
                "namespace_id": ns_str,
            }
        )

    return dict(meta_payload)


def _row_to_dict(raw_id: str, row: Any) -> dict[str, Any]:
    """Map one system_design_design_requests row to the public dict shape.

    Kept as a single conversion point so ``get_design_request`` and
    ``list_design_requests`` can never independently drift on field names --
    exactly the class of bug a second implementation invites.
    """
    room_spec = row["room_spec"]
    if not isinstance(room_spec, dict):
        room_spec = json.loads(room_spec or "{}")
    metadata = row["metadata"]
    if not isinstance(metadata, dict):
        metadata = json.loads(metadata or "{}")

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
    completed_at = row["completed_at"]
    completed_at_iso = (
        completed_at.isoformat() if hasattr(completed_at, "isoformat") else completed_at
    )

    return {
        "id": raw_id,
        "label": row["node_label"],
        "title": row["title"],
        "description": row["description"],
        "quote_id": row["quote_id"],
        "functional_location_id": row["functional_location_id"],
        "status": row["status"],
        "priority": row["priority"],
        "owner_id": row["owner_id"],
        "design_id": row["design_id"],
        "room_spec": room_spec,
        "metadata": metadata,
        "created_at": created_at_iso,
        "updated_at": updated_at_iso,
        "completed_at": completed_at_iso,
    }


async def get_design_request(
    conn: Any,
    namespace_id: UUID | str,
    request_id: str,
) -> dict[str, Any]:
    """Fetch a design request by its ID or label."""
    raw_id = clean_request_id(request_id)
    req_lbl = canonical_request_label(raw_id)
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    if conn is not None:
        row = await conn.fetchrow(
            """
            SELECT node_label, title, description, quote_id,
                   functional_location_id, status, priority, owner_id,
                   design_id, room_spec, metadata, completed_at,
                   created_at, updated_at
            FROM system_design_design_requests
            WHERE namespace_id = $1::uuid
              AND node_label = $2
            """,
            ns_str,
            req_lbl,
        )
        if row is None:
            raise DesignRequestNotFoundError(
                f"Design request {raw_id!r} not found in namespace {ns_str}"
            )

        return _row_to_dict(raw_id, row)

    bucket = _MEM_DESIGN_REQUESTS.get(ns_str, {})
    if raw_id not in bucket:
        raise DesignRequestNotFoundError(
            f"Design request {raw_id!r} not found in namespace {ns_str}"
        )
    return dict(bucket[raw_id])


async def list_design_requests(
    conn: Any,
    namespace_id: UUID | str,
    status: str | None = None,
    owner_id: str | None = None,
    quote_id: str | None = None,
    functional_location_id: str | None = None,
    priority: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List design requests in the intake queue with optional filters."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)
    fl_lbl = _format_fl_label(functional_location_id)

    if conn is not None:
        sql = """
            SELECT node_label, title, description, quote_id,
                   functional_location_id, status, priority, owner_id,
                   design_id, room_spec, metadata, completed_at,
                   created_at, updated_at
            FROM system_design_design_requests
            WHERE namespace_id = $1::uuid
        """
        params: list[Any] = [ns_str]
        idx = 2

        if status:
            sql += f" AND status = ${idx}"
            params.append(status.strip().lower())
            idx += 1
        if owner_id:
            sql += f" AND owner_id = ${idx}"
            params.append(owner_id.strip())
            idx += 1
        if quote_id:
            sql += f" AND quote_id = ${idx}"
            params.append(quote_id.strip())
            idx += 1
        if fl_lbl:
            sql += f" AND functional_location_id = ${idx}"
            params.append(fl_lbl)
            idx += 1
        if priority:
            sql += f" AND priority = ${idx}"
            params.append(priority.strip().lower())
            idx += 1
        if query:
            sql += f" AND (title ILIKE ${idx} OR description ILIKE ${idx})"
            params.append(f"%{query.strip()}%")
            idx += 1

        sql += f" ORDER BY updated_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
        params.extend([max(1, limit), max(0, offset)])

        rows = await conn.fetch(sql, *params)
        return [_row_to_dict(clean_request_id(r["node_label"]), r) for r in rows]

    bucket = _MEM_DESIGN_REQUESTS.get(ns_str, {})
    items: list[dict[str, Any]] = []
    for r in bucket.values():
        if status and r.get("status", "").lower() != status.lower():
            continue
        if owner_id and r.get("owner_id") != owner_id:
            continue
        if quote_id and r.get("quote_id") != quote_id:
            continue
        if fl_lbl and r.get("functional_location_id") != fl_lbl:
            continue
        if priority and r.get("priority", "").lower() != priority.lower():
            continue
        if query:
            q_lower = query.lower()
            t_match = q_lower in r.get("title", "").lower()
            d_match = q_lower in r.get("description", "").lower()
            if not (t_match or d_match):
                continue
        items.append(dict(r))

    return items[offset : offset + limit]


async def update_design_request(
    conn: Any,
    namespace_id: UUID | str,
    request_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    owner_id: str | None = None,
    room_spec: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update fields on an existing DESIGN_REQUEST."""
    existing = await get_design_request(conn, namespace_id, request_id)
    raw_id = clean_request_id(request_id)
    req_lbl = canonical_request_label(raw_id)
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    updated_status = existing["status"]
    if status is not None:
        s_val = status.strip().lower()
        if s_val not in ALLOWED_STATUSES:
            raise InvalidDesignRequestStatusError(
                f"Invalid status {status!r}. Must be one of {sorted(ALLOWED_STATUSES)}"
            )
        updated_status = s_val

    updated_priority = existing["priority"]
    if priority is not None:
        p_val = priority.strip().lower()
        if p_val not in ALLOWED_PRIORITIES:
            raise InvalidDesignRequestPayloadError(
                f"Invalid priority {priority!r}. Must be one of {sorted(ALLOWED_PRIORITIES)}"
            )
        updated_priority = p_val

    updated_title = title.strip() if title is not None else existing["title"]
    if not updated_title:
        raise InvalidDesignRequestPayloadError("Design request 'title' must not be blank.")

    updated_desc = description if description is not None else existing.get("description", "")
    updated_owner = owner_id if owner_id is not None else existing.get("owner_id")

    # If owner assigned and status was pending, transition to assigned
    if updated_owner and updated_status == STATUS_PENDING:
        updated_status = STATUS_ASSIGNED

    updated_room_spec = dict(existing.get("room_spec") or {})
    if room_spec is not None:
        updated_room_spec.update(room_spec)

    updated_meta = dict(existing.get("metadata") or {})
    if metadata is not None:
        updated_meta.update(metadata)

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    completed_at = existing.get("completed_at")
    completed_at_dt: datetime.datetime | None = None
    if updated_status == STATUS_COMPLETED and not completed_at:
        completed_at = now_iso
        completed_at_dt = now

    meta_payload = {
        "id": raw_id,
        "title": updated_title,
        "description": updated_desc,
        "quote_id": existing.get("quote_id"),
        "functional_location_id": existing.get("functional_location_id"),
        "status": updated_status,
        "priority": updated_priority,
        "owner_id": updated_owner,
        "design_id": existing.get("design_id"),
        "room_spec": updated_room_spec,
        "metadata": updated_meta,
        "created_at": existing.get("created_at"),
        "updated_at": now_iso,
        "completed_at": completed_at,
    }

    if conn is not None:
        await conn.execute(
            """
            UPDATE system_design_design_requests
            SET title       = $1,
                description = $2,
                status      = $3,
                priority    = $4,
                owner_id    = $5,
                room_spec   = $6::jsonb,
                metadata    = $7::jsonb,
                completed_at = COALESCE(completed_at, $8),
                updated_at  = NOW()
            WHERE namespace_id = $9::uuid
              AND node_label = $10
            """,
            updated_title,
            updated_desc,
            updated_status,
            updated_priority,
            updated_owner,
            json.dumps(updated_room_spec),
            json.dumps(updated_meta),
            completed_at_dt,
            ns_str,
            req_lbl,
        )

        if updated_owner != existing.get("owner_id"):
            owner_lbl = _format_employee_label(updated_owner)
            await conn.execute(
                """
                DELETE FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND subject_label = $2
                  AND predicate = $3
                """,
                ns_str,
                req_lbl,
                _PRED_ASSIGNED_TO,
            )
            if owner_lbl:
                await conn.execute(
                    """
                    INSERT INTO kg_edges
                        (subject_label, predicate, object_label, confidence, namespace_id)
                    VALUES ($1, $2, $3, $4, $5::uuid)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                    """,
                    req_lbl,
                    _PRED_ASSIGNED_TO,
                    owner_lbl,
                    _STRUCTURAL_CONFIDENCE,
                    ns_str,
                )

        if updated_status == STATUS_COMPLETED:
            await emit_graph_write(
                conn,
                namespace_id=ns_uuid,
                node_type=_NODE_TYPE_DESIGN_REQUEST,
                op="completed",
                node_id=req_lbl,
            )
        else:
            await emit_graph_write(
                conn,
                namespace_id=ns_uuid,
                node_type=_NODE_TYPE_DESIGN_REQUEST,
                op="updated",
                node_id=req_lbl,
            )

        return await get_design_request(conn, ns_uuid, raw_id)

    bucket = _MEM_DESIGN_REQUESTS.setdefault(ns_str, {})
    bucket[raw_id] = dict(meta_payload)

    if updated_owner != existing.get("owner_id"):
        edges = _MEM_REQUEST_EDGES.setdefault(ns_str, [])
        _MEM_REQUEST_EDGES[ns_str] = [
            e
            for e in edges
            if not (e["subject_label"] == req_lbl and e["predicate"] == _PRED_ASSIGNED_TO)
        ]
        owner_lbl = _format_employee_label(updated_owner)
        if owner_lbl:
            _MEM_REQUEST_EDGES[ns_str].append(
                {
                    "subject_label": req_lbl,
                    "predicate": _PRED_ASSIGNED_TO,
                    "object_label": owner_lbl,
                    "confidence": _STRUCTURAL_CONFIDENCE,
                    "namespace_id": ns_str,
                }
            )

    return dict(meta_payload)


async def assign_design_request(
    conn: Any,
    namespace_id: UUID | str,
    request_id: str,
    owner_id: str,
) -> dict[str, Any]:
    """Assign a design request to an owner (engineer)."""
    if not owner_id or not owner_id.strip():
        raise InvalidDesignRequestPayloadError("Assignee 'owner_id' must not be blank.")
    return await update_design_request(
        conn,
        namespace_id,
        request_id,
        owner_id=owner_id.strip(),
        status=STATUS_ASSIGNED,
    )


async def complete_design_request(
    conn: Any,
    namespace_id: UUID | str,
    request_id: str,
    design_id: str,
) -> dict[str, Any]:
    """Mark a design request as COMPLETED and link the resulting DESIGN."""
    if not design_id or not design_id.strip():
        raise InvalidDesignRequestPayloadError("Resulting 'design_id' must not be blank.")

    raw_id = clean_request_id(request_id)
    req_lbl = canonical_request_label(raw_id)
    d_clean = str(design_id).strip()
    if d_clean.upper().startswith("DESIGN:"):
        d_clean = d_clean[len("DESIGN:") :]
    d_lbl = f"DESIGN:{d_clean}"

    existing = await get_design_request(conn, namespace_id, request_id)
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ns_str = str(ns_uuid)

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    meta_payload = dict(existing)
    meta_payload["status"] = STATUS_COMPLETED
    meta_payload["design_id"] = d_clean
    meta_payload["completed_at"] = now_iso
    meta_payload["updated_at"] = now_iso

    if conn is not None:
        await conn.execute(
            """
            UPDATE system_design_design_requests
            SET status       = $1,
                design_id    = $2,
                completed_at = COALESCE(completed_at, $3),
                updated_at   = NOW()
            WHERE namespace_id = $4::uuid
              AND node_label = $5
            """,
            STATUS_COMPLETED,
            d_clean,
            now,
            ns_str,
            req_lbl,
        )

        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, $2, $3, $4, $5::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET updated_at = NOW()
            """,
            req_lbl,
            _PRED_REALIZED_AS,
            d_lbl,
            _STRUCTURAL_CONFIDENCE,
            ns_str,
        )

        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_DESIGN_REQUEST,
            op="completed",
            node_id=req_lbl,
        )

        return await get_design_request(conn, ns_uuid, raw_id)

    bucket = _MEM_DESIGN_REQUESTS.setdefault(ns_str, {})
    bucket[raw_id] = dict(meta_payload)

    edges = _MEM_REQUEST_EDGES.setdefault(ns_str, [])
    edges.append(
        {
            "subject_label": req_lbl,
            "predicate": _PRED_REALIZED_AS,
            "object_label": d_lbl,
            "confidence": _STRUCTURAL_CONFIDENCE,
            "namespace_id": ns_str,
        }
    )
    return dict(meta_payload)


async def cancel_design_request(
    conn: Any,
    namespace_id: UUID | str,
    request_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Cancel or reject a design request."""
    metadata = {"cancel_reason": reason} if reason else None
    return await update_design_request(
        conn,
        namespace_id,
        request_id,
        status=STATUS_CANCELLED,
        metadata=metadata,
    )


async def find_active_request_for_quote(
    conn: Any,
    namespace_id: UUID | str,
    quote_id: str,
) -> dict[str, Any] | None:
    """Find the earliest actionable (pending/assigned/in_progress) request for a quote."""
    requests = await list_design_requests(conn, namespace_id, quote_id=quote_id, limit=20)
    for r in requests:
        if r.get("status") in (STATUS_PENDING, STATUS_ASSIGNED, STATUS_IN_PROGRESS):
            return r
    return None


async def fulfill_design_request_from_quote(
    engine: Any,
    namespace_id: UUID | str,
    request_id: str,
    design_id: str | None = None,
    namespace_slug: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Fulfill an intake queue design request by realizing its quote into a DESIGN."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    raw_id = clean_request_id(request_id)

    has_pool = hasattr(engine, "pg_pool") and engine.pg_pool is not None
    if has_pool:
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            req = await get_design_request(conn, ns_uuid, raw_id)
    else:
        req = await get_design_request(None, ns_uuid, raw_id)

    if req.get("status") in TERMINAL_STATUSES:
        raise InvalidDesignRequestStatusError(
            f"Cannot fulfill design request {raw_id!r} in terminal status {req.get('status')!r}"
        )

    quote_id = req.get("quote_id")
    if not quote_id:
        raise InvalidDesignRequestPayloadError(
            f"Design request {raw_id!r} does not have an associated quote_id to fulfill from."
        )

    from nce.vertical_modules.system_design.from_quote import do_design_from_quote

    fq_params: dict[str, Any] = {
        "namespace_id": str(ns_uuid),
        "quote_id": quote_id,
        "design_id": design_id or f"DESIGN-{quote_id}",
        "design_request_id": raw_id,
    }
    if namespace_slug:
        fq_params["namespace_slug"] = namespace_slug
    if source_id:
        fq_params["source_id"] = source_id

    result = await do_design_from_quote(engine, fq_params)
    return result
