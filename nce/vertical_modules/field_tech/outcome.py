"""
nce/vertical_modules/field_tech/outcome.py
===========================================
Work Order Outcome Recording for Module 12 (Field Tech Engine).

Appends work order completion quality/rating to ``v3_cognitive_ledger``
tagged with ``field_tech_source_id`` for cognitive recall and downstream
consumption by Vendors (do_compute_performance) and HR (utilization/skill).

Strict Tenant Predicate Discipline (Charter §4.4)
-------------------------------------------------
EVERY query against v3_cognitive_ledger / work_orders carries an explicit
WHERE namespace_id = $N::uuid predicate.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.field_tech.outcome")

# Literal producer constant for gate checks
EVENT_TYPE_OUTCOME_RECORDED: str = "field_tech_outcome_recorded"
_MODEL_VERSION: str = "field_tech/v1"
_ZERO_TENSOR: list[float] = [0.0] * 6


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


async def do_record_outcome(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record completion outcome and quality score for a work order.

    Parameters
    ----------
    engine:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - work_order_id: (required) work order business identifier.
        - rating: (optional) float rating [1.0 - 5.0].
        - quality_score: (optional) float score [0.0 - 1.0].
        - resolution_notes: (optional) str completion / resolution notes.
        - was_rework: (optional) bool indicating if this was a rework visit.
        - completed_by: (optional) str technician or contractor ID.
        - mark_completed: (optional) bool, default True. If True, sets status='completed'.
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    rating = float(params.get("rating", 5.0))
    quality_score = float(params.get("quality_score", 1.0))
    resolution_notes = str(params.get("resolution_notes") or "").strip()
    was_rework = bool(params.get("was_rework", False))
    completed_by = str(params.get("completed_by") or "").strip()
    mark_completed = bool(params.get("mark_completed", True))

    ledger_id = uuid4()

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Fetch work order under lock with strict tenant predicate
        wo_row = await conn.fetchrow(
            """
            SELECT work_order_id, namespace_id, partner_scope_id, kind, assignee_id, assignee_kind, status, raw
            FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            FOR UPDATE
            """,
            work_order_id,
            ns_uuid,
        )
        if wo_row is None:
            raise ValueError(f"Work order {work_order_id!r} not found in namespace")

        effective_completed_by = completed_by or (wo_row["assignee_id"] or "unassigned")

        # 2. Update status to completed if requested
        if mark_completed:
            outcome_meta = {
                "outcome": {
                    "ledger_id": str(ledger_id),
                    "rating": rating,
                    "quality_score": quality_score,
                    "was_rework": was_rework,
                    "resolution_notes": resolution_notes,
                    "completed_by": effective_completed_by,
                }
            }
            await conn.execute(
                """
                UPDATE work_orders
                SET status = 'completed',
                    updated_at = NOW(),
                    raw = COALESCE(raw, '{}'::jsonb) || $3::jsonb
                WHERE work_order_id = $1 AND namespace_id = $2::uuid
                """,
                work_order_id,
                ns_uuid,
                json.dumps(outcome_meta),
            )

        # 3. Append to v3_cognitive_ledger with field_tech_source_id
        tlx_payload = {
            "event_type": "field_tech_outcome",
            "field_tech_source_id": work_order_id,
            "work_order_id": work_order_id,
            "kind": wo_row["kind"],
            "rating": rating,
            "quality_score": quality_score,
            "was_rework": was_rework,
            "completed_by": effective_completed_by,
            "assignee_kind": wo_row["assignee_kind"],
            "resolution_notes": resolution_notes,
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
                $5, NOW()
            )
            """,
            ledger_id,
            ns_uuid,
            _ZERO_TENSOR,
            json.dumps(tlx_payload),
            _MODEL_VERSION,
        )

    return {
        "status": "recorded",
        "ledger_id": str(ledger_id),
        "work_order_id": work_order_id,
        "rating": rating,
        "quality_score": quality_score,
        "completed_by": effective_completed_by,
        "marked_completed": mark_completed,
    }
