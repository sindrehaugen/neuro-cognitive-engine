"""
nce/vertical_modules/support/tickets.py
======================================
Core ticket domain logic for Module 10 (Support Engine):
  - do_open_ticket: creates native ServiceTicket and initialises running SLA clock
  - do_query_ticket: single ticket retrieval with SLA clock or filtered multi-ticket query
  - do_resolve_ticket: resolves ticket, updates SLA clock, appends auditable
    resolution to v3_cognitive_ledger for cognitive recall

Strict Tenant Predicate Discipline (Charter §5.5)
-------------------------------------------------
RLS is inert under mcp_user (rolsuper=true, rolbypassrls=true).
EVERY query in this module against tenant tables (service_tickets, sla_clocks,
v3_cognitive_ledger) carries an explicit WHERE namespace_id = $N::uuid predicate.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from nce.config import cfg
from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.support.tickets")

_ALLOWED_PRIORITIES = frozenset({"low", "medium", "high", "critical"})
_ALLOWED_SOURCES = frozenset({"nce", "d365"})
_ALLOWED_CHANGE_ORIGINS = frozenset(
    {
        "sync",
        "webhook",
        "agent",
        "operator",
        "consolidation",
        "replay",
        "proactive_telemetry",
        "proactive_health",
        "unknown",
    }
)
_ALLOWED_STATUSES = frozenset(
    {
        "open",
        "in_progress",
        "waiting_customer",
        "waiting_parts",
        "resolved",
        "closed",
        "cancelled",
    }
)

EVENT_TYPE_TICKET_OPENED: str = "support_ticket_opened"
EVENT_TYPE_TICKET_RESOLVED: str = "support_ticket_resolved"

_DEFAULT_SLA_TARGETS: dict[str, dict[str, datetime.timedelta]] = {
    "critical": {
        "first_response": datetime.timedelta(hours=1),
        "resolution": datetime.timedelta(hours=4),
    },
    "high": {
        "first_response": datetime.timedelta(hours=2),
        "resolution": datetime.timedelta(hours=8),
    },
    "medium": {
        "first_response": datetime.timedelta(hours=4),
        "resolution": datetime.timedelta(hours=24),
    },
    "low": {
        "first_response": datetime.timedelta(hours=8),
        "resolution": datetime.timedelta(hours=48),
    },
}

_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class TicketNotFoundError(Exception):
    """No service_tickets row exists for this (id, namespace_id) pair."""

    def __init__(self, *, ticket_id: str) -> None:
        self.ticket_id = ticket_id
        super().__init__(f"no service_tickets row for ticket_id={ticket_id!r}")


class TicketAlreadyResolvedError(Exception):
    """The ticket is already in 'resolved' status."""

    def __init__(self, *, ticket_id: str, status: str) -> None:
        self.ticket_id = ticket_id
        self.status = status
        super().__init__(f"ticket_id={ticket_id!r} is already resolved (status={status!r})")


class InvalidTicketStatusError(Exception):
    """Ticket status transition is invalid (e.g. attempting to resolve closed/cancelled)."""

    def __init__(self, *, ticket_id: str, status: str) -> None:
        self.ticket_id = ticket_id
        self.status = status
        super().__init__(f"ticket_id={ticket_id!r} with status={status!r} cannot be resolved")


class AutocloseConfidenceRefusalError(Exception):
    """Refusal when autonomous ticket resolution confidence is below the required threshold."""

    def __init__(self, *, confidence: float, threshold: float) -> None:
        self.confidence = confidence
        self.threshold = threshold
        super().__init__(
            f"Autoclose refused: confidence {confidence:.4f} is below autonomous threshold {threshold:.4f}"
        )


def _extract_pool(engine_or_pool: Any) -> Any:
    """Extract an asyncpg pool or pool-like object from engine or pool."""
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str) -> UUID:
    """Validate and parse a UUID string or instance."""
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an asyncpg Record or dictionary to a JSON-safe dictionary."""
    if row is None:
        return {}
    res: dict[str, Any] = {}
    items = row.items() if hasattr(row, "items") else row.items()
    for k, v in items:
        if k == "_full_count":
            continue
        if isinstance(v, UUID):
            res[k] = str(v)
        elif isinstance(v, (datetime.datetime, datetime.date)):
            res[k] = v.isoformat()
        else:
            res[k] = v
    return res


async def do_open_ticket(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Open a new native service ticket and initialize its running SLA clock.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - summary: (required) non-blank summary description.
        - priority: (optional) 'low', 'medium', 'high', 'critical' (default 'medium').
        - description: (optional) detailed ticket context.
        - source: (optional) 'nce' or 'd365' (default 'nce').
        - source_id: (optional) external system source reference.
        - asset_id: (optional) associated asset UUID.
        - room_id: (optional) functional location / room ID string.
        - customer_id: (optional) customer ID string.
        - sla_profile: (optional) SLA profile name (default 'standard').
        - change_origin: (optional) origin slug (default 'agent').
        - support_source_id: (optional) hard-retirement tracker tag.
        - ai_diagnosis: (optional) dict of AI diagnostics.
        - create_sla_clock: (optional) bool, whether to start SLA clock (default True).
        - id / ticket_id: (optional) predetermined UUID for determinism/replay.
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    summary = str(params.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary is required and cannot be blank")

    priority = str(params.get("priority") or "medium").lower()
    if priority not in _ALLOWED_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(_ALLOWED_PRIORITIES)}, got {priority!r}")

    source = str(params.get("source") or "nce").lower()
    if source not in _ALLOWED_SOURCES:
        raise ValueError(f"source must be one of {sorted(_ALLOWED_SOURCES)}, got {source!r}")

    change_origin = str(params.get("change_origin") or params.get("origin") or "agent").lower()
    if change_origin not in _ALLOWED_CHANGE_ORIGINS:
        raise ValueError(
            f"change_origin must be one of {sorted(_ALLOWED_CHANGE_ORIGINS)}, got {change_origin!r}"
        )

    sla_profile = str(params.get("sla_profile") or "standard")

    asset_id_raw = params.get("asset_id")
    asset_id = _parse_uuid(asset_id_raw, "asset_id") if asset_id_raw else None

    room_id = str(params.get("room_id")) if params.get("room_id") is not None else None
    customer_id = str(params.get("customer_id")) if params.get("customer_id") is not None else None
    source_id = str(params.get("source_id")) if params.get("source_id") is not None else None
    support_source_id = (
        str(params.get("support_source_id"))
        if params.get("support_source_id") is not None
        else None
    )
    description = str(params.get("description")) if params.get("description") is not None else None
    ai_diagnosis = params.get("ai_diagnosis") or {}

    ticket_id_raw = params.get("id") or params.get("ticket_id")
    ticket_id = _parse_uuid(ticket_id_raw, "ticket_id") if ticket_id_raw else uuid4()

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    initial_events = [
        {
            "type": "ticket_opened",
            "event_type": EVENT_TYPE_TICKET_OPENED,
            "at": now_dt.isoformat(),
            "origin": change_origin,
            "priority": priority,
        }
    ]

    async with scoped_pg_session(pool, ns_uuid) as conn:
        ticket_row = await conn.fetchrow(
            """
            INSERT INTO service_tickets (
                id, namespace_id, source, source_id, asset_id, room_id, customer_id,
                status, priority, summary, description, sla_profile,
                ai_diagnosis, events, support_source_id, change_origin,
                created_at, updated_at
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4, $5::uuid, $6, $7,
                'open', $8, $9, $10, $11,
                $12::jsonb, $13::jsonb, $14, $15,
                $16::timestamptz, $16::timestamptz
            )
            RETURNING *
            """,
            ticket_id,
            ns_uuid,
            source,
            source_id,
            asset_id,
            room_id,
            customer_id,
            priority,
            summary,
            description,
            sla_profile,
            json.dumps(ai_diagnosis),
            json.dumps(initial_events),
            support_source_id,
            change_origin,
            now_dt,
        )

        sla_row = None
        if params.get("create_sla_clock", True):
            sla_targets = _DEFAULT_SLA_TARGETS.get(priority, _DEFAULT_SLA_TARGETS["medium"])
            first_response_due = now_dt + sla_targets["first_response"]
            resolution_due = now_dt + sla_targets["resolution"]

            sla_row = await conn.fetchrow(
                """
                INSERT INTO sla_clocks (
                    ticket_id, namespace_id, sla_profile,
                    first_response_due, resolution_due,
                    breached, breach_type, paused_intervals, updated_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3,
                    $4::timestamptz, $5::timestamptz,
                    FALSE, NULL, '[]'::jsonb, $6::timestamptz
                )
                ON CONFLICT (ticket_id) DO NOTHING
                RETURNING *
                """,
                ticket_id,
                ns_uuid,
                sla_profile,
                first_response_due,
                resolution_due,
                now_dt,
            )

    return {
        "ticket": _row_to_dict(ticket_row),
        "sla_clock": _row_to_dict(sla_row) if sla_row else None,
    }


async def do_query_ticket(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve a single ticket with its SLA clock, or query a filtered list of tickets.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id / id: (optional) single ticket UUID. If present, returns single ticket.
        - status: (optional) filter by status ('open', 'resolved', etc.).
        - customer_id: (optional) filter by customer ID.
        - room_id: (optional) filter by room ID.
        - asset_id: (optional) filter by asset UUID.
        - priority: (optional) filter by priority.
        - limit: (optional) pagination limit (default 50, max 200).
        - offset: (optional) pagination offset (default 0).
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    ticket_id_raw = params.get("ticket_id") or params.get("id")

    # Path 1: Single ticket query
    if ticket_id_raw is not None:
        ticket_id = _parse_uuid(ticket_id_raw, "ticket_id")
        async with scoped_pg_session(pool, ns_uuid) as conn:
            ticket_row = await conn.fetchrow(
                """
                SELECT *
                FROM service_tickets
                WHERE id = $1::uuid AND namespace_id = $2::uuid
                """,
                ticket_id,
                ns_uuid,
            )
            if ticket_row is None:
                raise TicketNotFoundError(ticket_id=str(ticket_id))

            sla_row = await conn.fetchrow(
                """
                SELECT *
                FROM sla_clocks
                WHERE ticket_id = $1::uuid AND namespace_id = $2::uuid
                """,
                ticket_id,
                ns_uuid,
            )

        return {
            "ticket": _row_to_dict(ticket_row),
            "sla_clock": _row_to_dict(sla_row) if sla_row else None,
        }

    # Path 2: Multi-ticket listing with dynamic filters
    query_parts = ["WHERE namespace_id = $1::uuid"]
    binds: list[Any] = [ns_uuid]

    if "status" in params and params["status"]:
        status = str(params["status"]).lower()
        if status in _ALLOWED_STATUSES:
            binds.append(status)
            query_parts.append(f"status = ${len(binds)}")

    if "priority" in params and params["priority"]:
        priority = str(params["priority"]).lower()
        if priority in _ALLOWED_PRIORITIES:
            binds.append(priority)
            query_parts.append(f"priority = ${len(binds)}")

    if "customer_id" in params and params["customer_id"]:
        binds.append(str(params["customer_id"]))
        query_parts.append(f"customer_id = ${len(binds)}")

    if "room_id" in params and params["room_id"]:
        binds.append(str(params["room_id"]))
        query_parts.append(f"room_id = ${len(binds)}")

    if "asset_id" in params and params["asset_id"]:
        asset_uuid = _parse_uuid(params["asset_id"], "asset_id")
        binds.append(asset_uuid)
        query_parts.append(f"asset_id = ${len(binds)}::uuid")

    limit = min(max(int(params.get("limit", 50)), 1), 200)
    offset = max(int(params.get("offset", 0)), 0)

    binds.append(limit)
    limit_idx = len(binds)
    binds.append(offset)
    offset_idx = len(binds)

    where_clause = " AND ".join(query_parts)
    list_sql = f"""
        SELECT *, COUNT(*) OVER() as _full_count
        FROM service_tickets
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${limit_idx} OFFSET ${offset_idx}
    """

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(list_sql, *binds)

    total = rows[0]["_full_count"] if rows else 0
    return {
        "tickets": [_row_to_dict(r) for r in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


async def do_resolve_ticket(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Resolve an open ticket, update its SLA clock, and append to v3_cognitive_ledger.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id / id: (required) ticket UUID.
        - resolution_text: (required) non-blank text describing how the ticket was resolved.
        - was_fix: (optional) bool, whether the action resolved the root cause (default True).
        - resolution_category: (optional) slug ('hardware', 'firmware', 'configuration', etc.).
        - fixed_asset_id: (optional) asset UUID that was fixed.
        - fixed_product_id: (optional) product ID.
        - resolved_by: (optional) agent / operator ID.
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id = _parse_uuid(params.get("ticket_id") or params.get("id"), "ticket_id")

    resolution_text = str(params.get("resolution_text") or "").strip()
    if not resolution_text:
        raise ValueError("resolution_text is required and cannot be blank")

    # Autonomy Guard (Charter §6 & Spec §9.5):
    # Auto-closing a ticket without sufficient confidence is refused.
    autonomous = bool(params.get("autonomous", False))
    if autonomous:
        config_threshold = float(getattr(cfg, "NCE_SUPPORT_AUTONOMY_AUTOCLOSE_CONFIDENCE", 0.95))
        raw_threshold = params.get("autoclose_confidence")
        if raw_threshold is not None:
            # Caller may only tighten (raise) the confidence threshold, never lower it below config
            threshold = max(config_threshold, float(raw_threshold))
        else:
            threshold = config_threshold
        confidence = float(params.get("confidence", 0.0))
        if confidence < threshold:
            raise AutocloseConfidenceRefusalError(
                confidence=confidence,
                threshold=threshold,
            )

    was_fix = bool(params.get("was_fix", True))
    resolution_category = str(params.get("resolution_category") or "other")
    resolved_by = str(params.get("resolved_by") or "agent")
    fixed_asset_id = params.get("fixed_asset_id")
    fixed_product_id = params.get("fixed_product_id")

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    res_event = [
        {
            "type": "ticket_resolved",
            "event_type": EVENT_TYPE_TICKET_RESOLVED,
            "at": now_dt.isoformat(),
            "resolution_text": resolution_text,
            "was_fix": was_fix,
            "resolution_category": resolution_category,
            "resolved_by": resolved_by,
        }
    ]

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Fetch under lock with strict tenant predicate
        ticket_row = await conn.fetchrow(
            """
            SELECT *
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            FOR UPDATE
            """,
            ticket_id,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_id))

        if ticket_row["status"] == "resolved":
            raise TicketAlreadyResolvedError(ticket_id=str(ticket_id), status=ticket_row["status"])
        if ticket_row["status"] in ("closed", "cancelled"):
            raise InvalidTicketStatusError(ticket_id=str(ticket_id), status=ticket_row["status"])

        # 2. Update service_tickets to resolved
        updated_row = await conn.fetchrow(
            """
            UPDATE service_tickets
            SET status = 'resolved',
                resolved_at = $3::timestamptz,
                updated_at = $3::timestamptz,
                events = events || $4::jsonb
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            RETURNING *
            """,
            ticket_id,
            ns_uuid,
            now_dt,
            json.dumps(res_event),
        )

        # 3. Touch sla_clocks updated_at
        await conn.execute(
            """
            UPDATE sla_clocks
            SET updated_at = $3::timestamptz
            WHERE ticket_id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_id,
            ns_uuid,
            now_dt,
        )

        # 4. Append resolution fact to v3_cognitive_ledger
        ledger_id = uuid4()
        effective_asset_id = (
            str(fixed_asset_id)
            if fixed_asset_id
            else (str(ticket_row["asset_id"]) if ticket_row["asset_id"] else None)
        )
        tlx_payload = {
            "event_type": "ticket_resolution",
            "ticket_id": str(ticket_id),
            "summary": ticket_row["summary"],
            "resolution_text": resolution_text,
            "was_fix": was_fix,
            "resolution_category": resolution_category,
            "fixed_asset_id": effective_asset_id,
            "fixed_product_id": str(fixed_product_id) if fixed_product_id else None,
            "resolved_by": resolved_by,
        }

        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                id, namespace_id, memory_id,
                empathic_tensor, tlx_scores, vad_scores,
                model_version, created_at
            ) VALUES (
                $1::uuid, $2::uuid, NULL,
                $3::float[], $4::jsonb, '{}'::jsonb,
                $5, $6::timestamptz
            )
            """,
            ledger_id,
            ns_uuid,
            _ZERO_TENSOR,
            json.dumps(tlx_payload),
            "support/v1",
            now_dt,
        )

    return {
        "ticket": _row_to_dict(updated_row),
        "ledger_id": str(ledger_id),
        "status": "resolved",
    }
