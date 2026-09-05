"""
nce/vertical_modules/support/dispatch.py
========================================
Module 10 (Support Engine) — Work Order Dispatch & Boundary Edge Writer.

Implements B4 service dispatch:
  - Writes boundary edge: TICKET -[dispatched_as]-> WORK_ORDER (consumed by Field Tech 12).
  - Enforces Contract-A single-writer ownership invariant:
      Support asserts ownership for TICKET; Support does NOT own or create WORK_ORDER nodes.
  - Autonomous tier governed by DISPATCH_CEILING in nce/config.py:
      Over-ceiling dispatch MUST be refused unless confirmed by human operator.
  - Deterministic idempotency:
      Derives stable key from (namespace_id, ticket_id) so retries NEVER produce duplicate
      work orders or duplicate kg_edges.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid5

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketNotFoundError,
    _extract_pool,
    _parse_uuid,
)

log = logging.getLogger("nce.vertical_modules.support.dispatch")

_DISPATCH_NAMESPACE_UUID = UUID("e8b0a94d-1785-4081-9b16-564344d56789")
_SUPPORT_ENGINE = "support"
_NODE_TYPE_TICKET = "TICKET"


class DispatchCeilingExceededError(ValueError):
    """Raised when an autonomous dispatch exceeds the configured DISPATCH_CEILING."""

    def __init__(self, *, estimated_cost: float, ceiling: float) -> None:
        self.estimated_cost = estimated_cost
        self.ceiling = ceiling
        super().__init__(
            f"Dispatch estimated cost {estimated_cost:.2f} exceeds autonomous ceiling "
            f"{ceiling:.2f}; human confirmation required."
        )


def _derive_dispatch_idempotency_key(namespace_id: str, ticket_id: str) -> str:
    """Derive deterministic idempotency key for work order dispatch."""
    payload = json.dumps(
        {"namespace_id": str(namespace_id), "ticket_id": str(ticket_id)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return "dispatch:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _derive_work_order_id(namespace_id: UUID, ticket_id: UUID) -> UUID:
    """Derive deterministic work order UUID from (namespace, ticket)."""
    seed = f"{namespace_id}:{ticket_id}:work_order"
    return uuid5(_DISPATCH_NAMESPACE_UUID, seed)


async def do_dispatch_work_order(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a service ticket to Field Tech as a WORK_ORDER boundary edge.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id: (required) ticket UUID to dispatch.
        - estimated_cost / cost: (optional) estimated cost in tenant currency (default 0.0).
        - dispatch_ceiling: (optional) override ceiling for evaluation (defaults to config).
        - confirm: (optional) bool, human confirmation override for over-ceiling dispatch.
        - notes: (optional) technician dispatch notes.

    Returns
    -------
    dict with:
        - dispatched: bool
        - idempotent_replay: bool
        - ticket_id: str
        - work_order_id: str
        - edge: str ("TICKET:{tid} -[dispatched_as]-> WORK_ORDER:{wo_id}")
        - created_at: str
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")
    ticket_id = _parse_uuid(params.get("ticket_id") or params.get("id"), "ticket_id")

    confirm = bool(params.get("confirm", False))

    # Configuration is authoritative for DISPATCH_CEILING.
    # An optional caller-supplied dispatch_ceiling can only tighten (lower) the ceiling, never raise it.
    config_ceiling = float(getattr(cfg, "NCE_SUPPORT_AUTONOMY_DISPATCH_CEILING", 0.0))
    raw_ceiling = params.get("dispatch_ceiling")
    if raw_ceiling is not None:
        ceiling = min(config_ceiling, float(raw_ceiling))
    else:
        ceiling = config_ceiling

    # Autonomy Guard (Charter §6 & ML10b-Orch bypass fix):
    # An absent cost estimate is un-evaluable and MUST fail closed (require confirm=True).
    # Explicit 0.0 cost indicates genuinely zero-cost dispatch and proceeds autonomously.
    raw_cost = params.get("estimated_cost") if "estimated_cost" in params else params.get("cost")
    if raw_cost is None:
        if not confirm:
            raise DispatchCeilingExceededError(
                estimated_cost=float("inf"),
                ceiling=ceiling,
            )
        estimated_cost = 0.0
    else:
        estimated_cost = float(raw_cost)
        if not confirm and estimated_cost > ceiling:
            raise DispatchCeilingExceededError(
                estimated_cost=estimated_cost,
                ceiling=ceiling,
            )

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    ticket_subject = f"TICKET:{ticket_id}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Fetch ticket to verify existence, status, and tenant isolation
        ticket_row = await conn.fetchrow(
            """
            SELECT id, namespace_id, status, summary, events
            FROM service_tickets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            ticket_id,
            ns_uuid,
        )
        if ticket_row is None:
            raise TicketNotFoundError(ticket_id=str(ticket_id))

        status = ticket_row["status"]
        if status in ("resolved", "closed", "cancelled"):
            raise InvalidTicketStatusError(
                ticket_id=str(ticket_id),
                status=status,
            )

        # 2. Idempotency Check: Did we already dispatch this ticket?
        existing_edge = await conn.fetchrow(
            """
            SELECT object_label, created_at
            FROM kg_edges
            WHERE subject_label = $1 AND predicate = 'dispatched_as' AND namespace_id = $2::uuid
            """,
            ticket_subject,
            ns_uuid,
        )
        if existing_edge is not None:
            obj_label = existing_edge["object_label"]
            wo_id = obj_label.replace("WORK_ORDER:", "")
            created_at_val = existing_edge["created_at"]
            created_at_str = (
                created_at_val.isoformat()
                if hasattr(created_at_val, "isoformat")
                else str(created_at_val)
            )
            log.info(
                "Dispatch idempotent replay for ticket_id=%s -> work_order_id=%s",
                ticket_id,
                wo_id,
            )
            return {
                "dispatched": True,
                "idempotent_replay": True,
                "ticket_id": str(ticket_id),
                "work_order_id": wo_id,
                "edge": f"{ticket_subject} -[dispatched_as]-> {obj_label}",
                "created_at": created_at_str,
            }

        # 3. Contract A Ownership Guard: Assert Support owns TICKET
        # (Support does NOT own or touch WORK_ORDER node)
        await assert_owner(conn, ns_uuid, _NODE_TYPE_TICKET, _SUPPORT_ENGINE)

        # 4. Derive deterministic work_order_id
        work_order_id = _derive_work_order_id(ns_uuid, ticket_id)
        wo_object = f"WORK_ORDER:{work_order_id}"

        # 5. Insert boundary edge into kg_edges
        edge_row = await conn.fetchrow(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, 'dispatched_as', $2, 1.0, $3::uuid, 'agent'
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            RETURNING created_at
            """,
            ticket_subject,
            wo_object,
            ns_uuid,
        )

        edge_created_at = (
            edge_row["created_at"].isoformat()
            if edge_row and hasattr(edge_row["created_at"], "isoformat")
            else now_dt.isoformat()
        )

        # 6. Append dispatch event to service_tickets events audit trail
        dispatch_event = {
            "type": "work_order_dispatched",
            "work_order_id": str(work_order_id),
            "estimated_cost": estimated_cost,
            "at": now_dt.isoformat(),
            "change_origin": "agent",
        }
        await conn.execute(
            """
            UPDATE service_tickets
            SET events = events || $1::jsonb,
                updated_at = $2::timestamptz
            WHERE id = $3::uuid AND namespace_id = $4::uuid
            """,
            json.dumps([dispatch_event]),
            now_dt,
            ticket_id,
            ns_uuid,
        )

    return {
        "dispatched": True,
        "idempotent_replay": False,
        "ticket_id": str(ticket_id),
        "work_order_id": str(work_order_id),
        "edge": f"{ticket_subject} -[dispatched_as]-> {wo_object}",
        "estimated_cost": estimated_cost,
        "created_at": edge_created_at,
    }
