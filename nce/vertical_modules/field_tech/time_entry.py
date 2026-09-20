"""
nce/vertical_modules/field_tech/time_entry.py
=============================================
Time tracking domain logic for Module 12 (Field Tech Engine):
  - do_log_time: records GPS-derived or manual labor spans with offline-sync op_id dedup,
    persisting to time_entries table and FIELD_TECH_TIME_ENTRY graph node.

Strict Tenant Predicate Discipline (Charter Â§4.4)
-------------------------------------------------
EVERY query against time_entries / work_orders carries explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.governor import governed
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner

log = logging.getLogger("nce.vertical_modules.field_tech.time_entry")

EVENT_TYPE_TIME_LOGGED: str = "field_tech_time_logged"
_NODE_TYPE_TIME_ENTRY = "FIELD_TECH_TIME_ENTRY"
_OWNER_ENGINE = "field_tech"
_ALLOWED_SOURCES = frozenset({"gps", "manual"})


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str) -> UUID:
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


def _parse_dt(val: Any, field_name: str) -> datetime.datetime:
    if isinstance(val, datetime.datetime):
        return val
    if not val:
        raise ValueError(f"{field_name} is required")
    try:
        return datetime.datetime.fromisoformat(str(val))
    except Exception as exc:
        raise ValueError(f"Invalid {field_name} timestamp: {val!r}") from exc


async def do_log_time(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Log labor time for a work order from manual entry or GPS geofence tracking."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    op_id = params.get("op_id")
    if op_id:
        op_id = str(op_id).strip()

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # Check op_id idempotency
        if op_id:
            existing = await conn.fetchrow(
                """
                SELECT id, time_entry_id, work_order_id, namespace_id, started_at, ended_at, source, approved, op_id
                FROM time_entries
                WHERE op_id = $1 AND namespace_id = $2::uuid
                """,
                op_id,
                ns_uuid,
            )
            if existing:
                res = dict(existing)
                res["id"] = str(res["id"])
                res["namespace_id"] = str(res["namespace_id"])
                if res.get("started_at"):
                    res["started_at"] = res["started_at"].isoformat()
                if res.get("ended_at"):
                    res["ended_at"] = res["ended_at"].isoformat()
                res["deduplicated"] = True
                res["status"] = "deduplicated"
                return res

    source = str(params.get("source") or "manual").lower()
    if source not in _ALLOWED_SOURCES:
        raise ValueError(f"source must be one of {sorted(_ALLOWED_SOURCES)}, got {source!r}")

    # Started and ended time
    started_at_raw = params.get("started_at") or params.get("start")
    hours = params.get("hours")
    if not started_at_raw and hours is not None:
        ended_at = datetime.datetime.now(datetime.timezone.utc)
        started_at = ended_at - datetime.timedelta(hours=float(hours))
    else:
        started_at = _parse_dt(started_at_raw, "started_at")
        ended_at_raw = params.get("ended_at") or params.get("end")
        ended_at = _parse_dt(ended_at_raw, "ended_at") if ended_at_raw else None

    time_entry_id = params.get("time_entry_id")
    if time_entry_id:
        time_entry_id = str(time_entry_id).strip()
    else:
        time_entry_id = f"TE-{uuid4().hex[:8].upper()}"

    approved = bool(params.get("approved", False))
    raw = params.get("raw") or {}
    if "gps_track" in params:
        raw["gps_track"] = params["gps_track"]

    partner_scope_id = params.get("partner_scope_id")
    partner_scope_uuid: UUID | None = None
    if partner_scope_id:
        partner_scope_uuid = _parse_uuid(partner_scope_id, "partner_scope_id")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # Assert work order exists
        wo = await conn.fetchrow(
            """
            SELECT partner_scope_id FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            """,
            work_order_id,
            ns_uuid,
        )
        if wo is None:
            raise ValueError(f"Work order {work_order_id!r} not found in namespace")

        if partner_scope_uuid is None and wo["partner_scope_id"] is not None:
            partner_scope_uuid = wo["partner_scope_id"]

        row = await conn.fetchrow(
            """
            INSERT INTO time_entries (
                time_entry_id,
                work_order_id,
                namespace_id,
                partner_scope_id,
                started_at,
                ended_at,
                source,
                approved,
                op_id,
                raw,
                created_at,
                updated_at
            ) VALUES (
                $1, $2, $3::uuid, $4::uuid, $5, $6, $7, $8, $9, $10::jsonb, NOW(), NOW()
            )
            RETURNING
                id, time_entry_id, work_order_id, namespace_id, partner_scope_id,
                started_at, ended_at, source, approved, op_id, raw, created_at, updated_at
            """,
            time_entry_id,
            work_order_id,
            ns_uuid,
            partner_scope_uuid,
            started_at,
            ended_at,
            source,
            approved,
            op_id,
            json.dumps(raw),
        )

        # Graph node & edge
        te_label = f"{_NODE_TYPE_TIME_ENTRY}:{time_entry_id}"
        wo_label = f"WORK_ORDER:{work_order_id}"

        await assert_owner(conn, ns_uuid, _NODE_TYPE_TIME_ENTRY, _OWNER_ENGINE)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            te_label,
            _NODE_TYPE_TIME_ENTRY,
            ns_uuid,
        )

        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'has_time_entry', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            wo_label,
            te_label,
            ns_uuid,
        )

    res = dict(row)
    res["id"] = str(res["id"])
    res["namespace_id"] = str(res["namespace_id"])
    if res.get("partner_scope_id"):
        res["partner_scope_id"] = str(res["partner_scope_id"])
    if res.get("started_at"):
        res["started_at"] = res["started_at"].isoformat()
    if res.get("ended_at"):
        res["ended_at"] = res["ended_at"].isoformat()
    res["deduplicated"] = False
    res["status"] = "logged"
    return res


class TimeEntryNotFoundError(ValueError, KeyError):
    """Raised when the referenced time entry does not exist in this namespace."""


def _time_entry_idempotency_key(namespace_id: Any, time_entry_id: Any) -> str:
    """Deterministic default idempotency key: approving the same entry twice
    (no caller-supplied key) is the same governed action, not two."""
    return f"field_tech_approve_time_entry:{namespace_id}:{time_entry_id}"


@governed(action_type="field_tech_approve_time_entry")
async def do_approve_time_entry(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    engine: Any = None,
    time_entry_id: Any,
) -> dict[str, Any]:
    """Governed confirm-first approval of one time entry (C-7).

    Charter C-7 asked for "POST /{id}/approve (governed) -> Economy labour
    cost". Before this, ``approved`` was a plain ``writable_fields`` entry on
    FIELD_TECH_TIME_ENTRY_SPEC -- any caller with PATCH access could flip it
    with no confirmation and no audit trail, which is not what "governed"
    means. This is the confirm-first replacement; ``approved`` is removed
    from ``writable_fields`` in the same change (resources.py) so PATCH can
    no longer walk around this gate -- a governed front door beside an
    unlocked window guards nothing.

    Modelled on ``procurement/po.py``'s ``do_generate_po`` -- the caller
    (the admin route) opens ``scoped_pg_session`` and passes ``conn`` in;
    ``@governed`` requires ``conn`` already inside that transaction (see its
    own "Transaction guard"), never opens one itself.

    Without ``confirm=True``, ``@governed`` returns
    ``{"status": "pending_approval", ...}`` and this body never runs.
    Idempotent on ``idempotency_key``: a defaulted key
    (``_time_entry_idempotency_key``) makes a second confirm of the same
    entry a no-op replay rather than a second audited action, the same
    idempotency shape ``do_log_time`` already gives op_id-keyed logging.

    Raises
    ------
    TimeEntryNotFoundError
        No time entry with this id exists in this namespace.
    ValueError
        ``time_entry_id`` missing or unparseable.
    """
    ns_uuid = _parse_uuid(namespace_id, "namespace_id")
    te_uuid = _parse_uuid(time_entry_id, "time_entry_id")

    row = await conn.fetchrow(
        """
        UPDATE time_entries
        SET approved = TRUE, updated_at = NOW()
        WHERE id = $1 AND namespace_id = $2::uuid
        RETURNING id, time_entry_id, work_order_id, namespace_id, partner_scope_id,
                  started_at, ended_at, source, approved, op_id, created_at, updated_at
        """,
        te_uuid,
        ns_uuid,
    )
    if row is None:
        raise TimeEntryNotFoundError(
            f"Time entry {time_entry_id} not found in namespace {namespace_id}"
        )

    res = dict(row)
    res["id"] = str(res["id"])
    res["namespace_id"] = str(res["namespace_id"])
    if res.get("partner_scope_id"):
        res["partner_scope_id"] = str(res["partner_scope_id"])
    if res.get("started_at"):
        res["started_at"] = res["started_at"].isoformat()
    if res.get("ended_at"):
        res["ended_at"] = res["ended_at"].isoformat()
    res["status"] = "approved"
    return res
