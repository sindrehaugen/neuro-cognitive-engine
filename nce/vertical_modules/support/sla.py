"""
nce/vertical_modules/support/sla.py
===================================
Deterministic SLA clock calculations and monitoring for Module 10 (Support Engine):
  - load_sla_profiles: reads room-centric SLA targets from support-sla-profiles.json
  - calculate_sla_targets: calculates first-response and resolution deadlines
  - evaluate_sla_status: computes countdowns, pause deductions, breach status & risk
  - do_sla_clock: queries and refreshes running SLA clock state under strict tenant predicate
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.events.bus import publish
from nce.vertical_modules.support.tickets import (
    TicketNotFoundError,
    _extract_pool,
    _parse_uuid,
)

log = logging.getLogger("nce.vertical_modules.support.sla")

_CONFIG_DATA_DIR = Path(__file__).parents[2] / "config_data"

_FALLBACK_PROFILES: dict[str, dict[str, dict[str, float]]] = {
    "mission_critical": {
        "critical": {"first_response_hours": 0.5, "resolution_hours": 2.0},
        "high": {"first_response_hours": 1.0, "resolution_hours": 4.0},
        "medium": {"first_response_hours": 2.0, "resolution_hours": 8.0},
        "low": {"first_response_hours": 4.0, "resolution_hours": 16.0},
    },
    "standard": {
        "critical": {"first_response_hours": 1.0, "resolution_hours": 4.0},
        "high": {"first_response_hours": 2.0, "resolution_hours": 8.0},
        "medium": {"first_response_hours": 4.0, "resolution_hours": 24.0},
        "low": {"first_response_hours": 8.0, "resolution_hours": 48.0},
    },
    "basic": {
        "critical": {"first_response_hours": 2.0, "resolution_hours": 8.0},
        "high": {"first_response_hours": 4.0, "resolution_hours": 16.0},
        "medium": {"first_response_hours": 8.0, "resolution_hours": 48.0},
        "low": {"first_response_hours": 16.0, "resolution_hours": 72.0},
    },
    "best_effort": {
        "critical": {"first_response_hours": 4.0, "resolution_hours": 24.0},
        "high": {"first_response_hours": 8.0, "resolution_hours": 48.0},
        "medium": {"first_response_hours": 16.0, "resolution_hours": 72.0},
        "low": {"first_response_hours": 24.0, "resolution_hours": 120.0},
    },
}


def load_sla_profiles() -> dict[str, Any]:
    """Load SLA targets from nce/config_data/support-sla-profiles.json."""
    config_file = _CONFIG_DATA_DIR / "support-sla-profiles.json"
    if not config_file.exists():
        return _FALLBACK_PROFILES
    try:
        with open(config_file, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("profiles", _FALLBACK_PROFILES)
    except Exception as exc:
        log.warning("Failed to load SLA profiles config, using fallback: %s", exc)
        return _FALLBACK_PROFILES


def calculate_sla_targets(
    sla_profile: str,
    priority: str,
    base_time: datetime.datetime,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Calculate (first_response_due, resolution_due) based on profile and priority."""
    profiles = load_sla_profiles()
    profile_dict = profiles.get(
        sla_profile.lower(), profiles.get("standard", _FALLBACK_PROFILES["standard"])
    )
    target = profile_dict.get(
        priority.lower(),
        profile_dict.get("medium", {"first_response_hours": 4.0, "resolution_hours": 24.0}),
    )

    resp_delta = datetime.timedelta(hours=float(target.get("first_response_hours", 4.0)))
    res_delta = datetime.timedelta(hours=float(target.get("resolution_hours", 24.0)))

    return base_time + resp_delta, base_time + res_delta


def _ensure_dt(v: datetime.datetime | str | None) -> datetime.datetime | None:
    if v is None:
        return None
    if isinstance(v, str):
        dt = datetime.datetime.fromisoformat(v)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)
    return v if v.tzinfo is not None else v.replace(tzinfo=datetime.timezone.utc)


def evaluate_sla_status(
    *,
    first_response_due: datetime.datetime | str | None,
    resolution_due: datetime.datetime | str | None,
    first_response_at: datetime.datetime | str | None = None,
    resolved_at: datetime.datetime | str | None = None,
    paused_intervals: list[dict[str, Any]] | None = None,
    now: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """Compute deterministic countdowns, pause deductions, breach status and breach risk.

    Parameters
    ----------
    first_response_due: target timestamp for initial operator response.
    resolution_due: target timestamp for ticket resolution.
    first_response_at: actual timestamp when first response occurred.
    resolved_at: actual timestamp when ticket was resolved.
    paused_intervals: list of interval dicts with duration_seconds or paused_at/resumed_at.
    now: reference timestamp (default current UTC time).
    """
    now_dt = _ensure_dt(now) or datetime.datetime.now(datetime.timezone.utc)
    first_due_dt = _ensure_dt(first_response_due)
    res_due_dt = _ensure_dt(resolution_due)
    first_resp_dt = _ensure_dt(first_response_at)
    resolved_dt = _ensure_dt(resolved_at)

    # 1. Deduct paused time
    paused_seconds = 0
    is_paused = False
    if paused_intervals:
        for interval in paused_intervals:
            if "duration_seconds" in interval:
                paused_seconds += int(interval["duration_seconds"])
            elif interval.get("paused_at"):
                paused_dt = _ensure_dt(interval["paused_at"])
                if paused_dt is not None:
                    if interval.get("resumed_at"):
                        resumed_dt = _ensure_dt(interval["resumed_at"])
                        if resumed_dt is not None:
                            paused_seconds += max(int((resumed_dt - paused_dt).total_seconds()), 0)
                    else:
                        # currently active pause
                        is_paused = True
                        paused_seconds += max(int((now_dt - paused_dt).total_seconds()), 0)

    pause_delta = datetime.timedelta(seconds=paused_seconds)

    # 2. Check first response breach
    first_resp_breached = False
    effective_first_due = (first_due_dt + pause_delta) if first_due_dt else None
    remaining_first_resp = None

    if effective_first_due:
        if first_resp_dt is not None:
            first_resp_breached = first_resp_dt > effective_first_due
            remaining_first_resp = 0.0
        else:
            first_resp_breached = now_dt > effective_first_due
            remaining_first_resp = max((effective_first_due - now_dt).total_seconds(), 0.0)

    # 3. Check resolution breach
    res_breached = False
    effective_res_due = (res_due_dt + pause_delta) if res_due_dt else None
    remaining_res = None

    if effective_res_due:
        if resolved_dt is not None:
            res_breached = resolved_dt > effective_res_due
            remaining_res = 0.0
        else:
            res_breached = now_dt > effective_res_due
            remaining_res = max((effective_res_due - now_dt).total_seconds(), 0.0)

    # 4. Synthesize breach status
    breached = first_resp_breached or res_breached
    breach_type = None
    if first_resp_breached and res_breached:
        breach_type = "both"
    elif first_resp_breached:
        breach_type = "first_response"
    elif res_breached:
        breach_type = "resolution"

    # 5. Breach risk detection (approaching deadline within 1 hour or <20% time remaining)
    breach_risk = False
    if not breached:
        if remaining_first_resp is not None and first_response_at is None:
            if 0 < remaining_first_resp <= 1800:  # <= 30 mins
                breach_risk = True
        if remaining_res is not None and resolved_at is None:
            if 0 < remaining_res <= 3600:  # <= 1 hour
                breach_risk = True

    return {
        "breached": breached,
        "breach_type": breach_type,
        "breach_risk": breach_risk,
        "is_paused": is_paused,
        "paused_seconds": paused_seconds,
        "remaining_first_response_seconds": remaining_first_resp,
        "remaining_resolution_seconds": remaining_res,
        "effective_first_response_due": effective_first_due.isoformat()
        if effective_first_due
        else None,
        "effective_resolution_due": effective_res_due.isoformat() if effective_res_due else None,
    }


async def do_sla_clock(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Query and refresh running SLA clock state for a ticket.

    Carries explicit WHERE namespace_id = $N::uuid predicates on all tables.
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id = _parse_uuid(params.get("ticket_id") or params.get("id"), "ticket_id")

    now = params.get("now")
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now, str):
        now = datetime.datetime.fromisoformat(now)

    async with scoped_pg_session(pool, ns_uuid) as conn:
        ticket_row = await conn.fetchrow(
            """
            SELECT id, namespace_id, status, priority, sla_profile,
                   first_response_at, resolved_at, created_at
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
            SELECT ticket_id, namespace_id, sla_profile,
                   first_response_due, resolution_due,
                   breached, breach_type, paused_intervals, updated_at
            FROM sla_clocks
            WHERE ticket_id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_id,
            ns_uuid,
        )

        if sla_row is None:
            # Seed SLA clock if not already initialized
            resp_due, res_due = calculate_sla_targets(
                ticket_row["sla_profile"], ticket_row["priority"], ticket_row["created_at"]
            )
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
                ON CONFLICT (ticket_id) DO UPDATE SET updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                ticket_id,
                ns_uuid,
                ticket_row["sla_profile"],
                resp_due,
                res_due,
                now,
            )

        paused_intervals = sla_row["paused_intervals"]
        if isinstance(paused_intervals, str):
            paused_intervals = json.loads(paused_intervals)

        status_eval = evaluate_sla_status(
            first_response_due=sla_row["first_response_due"],
            resolution_due=sla_row["resolution_due"],
            first_response_at=ticket_row["first_response_at"],
            resolved_at=ticket_row["resolved_at"],
            paused_intervals=paused_intervals,
            now=now,
        )

        # Update sla_clocks if breach status changed
        if (
            status_eval["breached"] != sla_row["breached"]
            or status_eval["breach_type"] != sla_row["breach_type"]
        ):
            await conn.execute(
                """
                UPDATE sla_clocks
                SET breached = $3,
                    breach_type = $4,
                    updated_at = $5::timestamptz
                WHERE ticket_id = $1::uuid AND namespace_id = $2::uuid
                """,
                ticket_id,
                ns_uuid,
                status_eval["breached"],
                status_eval["breach_type"],
                now,
            )

    return {
        "ticket_id": str(ticket_id),
        "namespace_id": str(ns_uuid),
        "sla_profile": sla_row["sla_profile"],
        "first_response_due": sla_row["first_response_due"].isoformat()
        if sla_row["first_response_due"]
        else None,
        "resolution_due": sla_row["resolution_due"].isoformat()
        if sla_row["resolution_due"]
        else None,
        "first_response_at": ticket_row["first_response_at"].isoformat()
        if ticket_row["first_response_at"]
        else None,
        "resolved_at": ticket_row["resolved_at"].isoformat() if ticket_row["resolved_at"] else None,
        **status_eval,
    }


_NODE_TYPE_TICKET: str = "TICKET"
_OP_SLA_BREACHED: str = "sla_breached"


async def do_check_sla_breaches(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Scan open service tickets in active namespace and idempotently emit TICKET.sla_breached.

    Periodically called by the Support SLA breach watcher on cron (Wave SU-2).
    For each open ticket, evaluates first-response and resolution deadlines,
    updates persistent sla_clocks rows, appends audit records to service_tickets.events,
    and publishes TICKET.sla_breached to the C4 transactional outbox.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) Tenant UUID string or UUID.
        - now: (optional) Reference timestamp (defaults to current UTC time).
        - ticket_id: (optional) Single ticket UUID to restrict the check.

    Returns
    -------
    dict with:
        - namespace_id: str
        - checked: int
        - breached: int (new or escalated breaches detected and published in this run)
        - published: int
        - total_active_breaches: int (total open tickets currently in breached state)
        - breaches: list[dict]
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    now_val = params.get("now")
    if now_val is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now_val, str):
        now = datetime.datetime.fromisoformat(now_val)
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
    elif isinstance(now_val, datetime.datetime):
        now = (
            now_val if now_val.tzinfo is not None else now_val.replace(tzinfo=datetime.timezone.utc)
        )
    else:
        now = datetime.datetime.now(datetime.timezone.utc)

    ticket_id_raw = params.get("ticket_id") or params.get("id")
    target_ticket_id = _parse_uuid(ticket_id_raw, "ticket_id") if ticket_id_raw else None

    checked_count = 0
    newly_breached_count = 0
    total_active_breaches = 0
    published_count = 0
    breached_tickets: list[dict[str, Any]] = []

    async with scoped_pg_session(pool, ns_uuid) as conn:
        if target_ticket_id:
            ticket_rows = await conn.fetch(
                """
                SELECT t.id, t.status, t.priority, t.sla_profile,
                       t.first_response_at, t.resolved_at, t.created_at,
                       c.first_response_due, c.resolution_due, c.breached, c.breach_type,
                       c.paused_intervals
                FROM service_tickets t
                LEFT JOIN sla_clocks c ON c.ticket_id = t.id AND c.namespace_id = t.namespace_id
                WHERE t.namespace_id = $1::uuid AND t.id = $2::uuid
                """,
                ns_uuid,
                target_ticket_id,
            )
        else:
            ticket_rows = await conn.fetch(
                """
                SELECT t.id, t.status, t.priority, t.sla_profile,
                       t.first_response_at, t.resolved_at, t.created_at,
                       c.first_response_due, c.resolution_due, c.breached, c.breach_type,
                       c.paused_intervals
                FROM service_tickets t
                LEFT JOIN sla_clocks c ON c.ticket_id = t.id AND c.namespace_id = t.namespace_id
                WHERE t.namespace_id = $1::uuid
                  AND t.status IN ('open', 'in_progress', 'waiting_customer', 'waiting_parts')
                ORDER BY t.created_at ASC
                """,
                ns_uuid,
            )

        checked_count = len(ticket_rows)

        for row in ticket_rows:
            t_id = row["id"]
            sla_prof = row["sla_profile"] or "standard"
            priority = row["priority"] or "medium"
            created_at = row["created_at"]
            resp_due = row["first_response_due"]
            res_due = row["resolution_due"]
            was_breached = bool(row["breached"]) if row["breached"] is not None else False
            old_breach_type = row["breach_type"]
            raw_paused = row["paused_intervals"]

            if resp_due is None or res_due is None:
                # Seed missing sla_clocks targets
                calc_resp, calc_res = calculate_sla_targets(sla_prof, priority, created_at)
                resp_due = resp_due or calc_resp
                res_due = res_due or calc_res
                await conn.execute(
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
                    """,
                    t_id,
                    ns_uuid,
                    sla_prof,
                    resp_due,
                    res_due,
                    now,
                )

            paused_intervals = raw_paused
            if isinstance(paused_intervals, str):
                try:
                    paused_intervals = json.loads(paused_intervals)
                except Exception:
                    paused_intervals = []
            elif not isinstance(paused_intervals, list):
                paused_intervals = []

            status_eval = evaluate_sla_status(
                first_response_due=resp_due,
                resolution_due=res_due,
                first_response_at=row["first_response_at"],
                resolved_at=row["resolved_at"],
                paused_intervals=paused_intervals,
                now=now,
            )

            is_breached = status_eval["breached"]
            new_breach_type = status_eval["breach_type"]

            if is_breached:
                total_active_breaches += 1

                # A new breach or an escalated breach (e.g. first_response -> both)
                is_new_breach = (not was_breached) or (new_breach_type != old_breach_type)
                if is_new_breach:
                    # 🔴 One transaction for all three writes. Without it asyncpg
                    # autocommits each statement, so `sla_clocks.breached = TRUE`
                    # lands BEFORE the outbox row. A crash in between leaves the
                    # ticket marked breached with no event, and the idempotency
                    # guard above then evaluates is_new_breach = False forever --
                    # the breach is lost permanently. nce/events/bus.py:publish
                    # states the requirement outright: "Callers must call this
                    # inside an open transaction so post-commit semantics are
                    # preserved." Per ticket, not per scan: one bad ticket must
                    # not roll back the whole sweep.
                    async with conn.transaction():
                        newly_breached_count += 1

                        # 1. Update sla_clocks
                        await conn.execute(
                            """
                            UPDATE sla_clocks
                            SET breached = TRUE,
                                breach_type = $3,
                                updated_at = $4::timestamptz
                            WHERE ticket_id = $1::uuid AND namespace_id = $2::uuid
                            """,
                            t_id,
                            ns_uuid,
                            new_breach_type,
                            now,
                        )

                        # 2. Append breach audit event to service_tickets.events
                        breach_event = {
                            "type": "sla_breached",
                            "breach_type": new_breach_type,
                            "at": now.isoformat(),
                            "change_origin": "cron",
                        }
                        await conn.execute(
                            """
                            UPDATE service_tickets
                            SET events = events || $1::jsonb,
                                updated_at = $2::timestamptz
                            WHERE id = $3::uuid AND namespace_id = $4::uuid
                            """,
                            json.dumps([breach_event]),
                            now,
                            t_id,
                            ns_uuid,
                        )

                        # 3. Publish C4 transactional outbox event
                        ticket_subject = f"TICKET:{t_id}"
                        await publish(
                            conn,
                            namespace_id=ns_uuid,
                            node_type=_NODE_TYPE_TICKET,
                            op=_OP_SLA_BREACHED,
                            aggregate_id=ticket_subject,
                            payload={
                                "ticket_id": str(t_id),
                                "namespace_id": str(ns_uuid),
                                "breach_type": new_breach_type,
                                "sla_profile": sla_prof,
                                "priority": priority,
                                "first_response_due": status_eval["effective_first_response_due"],
                                "resolution_due": status_eval["effective_resolution_due"],
                                "breached_at": now.isoformat(),
                                "status": row["status"],
                            },
                        )
                        published_count += 1

                breached_tickets.append(
                    {
                        "ticket_id": str(t_id),
                        "breach_type": new_breach_type,
                        "newly_breached": is_new_breach,
                        "sla_profile": sla_prof,
                        "priority": priority,
                        "first_response_due": status_eval["effective_first_response_due"],
                        "resolution_due": status_eval["effective_resolution_due"],
                        "status": row["status"],
                    }
                )

    return {
        "namespace_id": str(ns_uuid),
        "checked": checked_count,
        "breached": newly_breached_count,
        "published": published_count,
        "total_active_breaches": total_active_breaches,
        "breaches": breached_tickets,
    }
