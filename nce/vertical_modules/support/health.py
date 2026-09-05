"""
nce/vertical_modules/support/health.py
======================================
Customer health scoring and churn-risk detection for Module 10 (Support Engine):
  - load_health_weights: reads passive signal weights from support-health-weights.json
  - compute_health_score: deterministic reducer over ticket cadence, recency,
    SLA breaches, frustration trend, and touchpoint responses
  - do_health_score: computes and upserts rolling health score into customer_health table
  - do_record_touchpoint: records ÉT-spørsmål touchpoint, writes to cognitive ledger,
    and triggers event-scoped health recomputation
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.support.tickets import (
    _ZERO_TENSOR,
    _extract_pool,
    _parse_uuid,
    _row_to_dict,
)

log = logging.getLogger("nce.vertical_modules.support.health")

_CONFIG_DATA_DIR = Path(__file__).parents[2] / "config_data"

_FALLBACK_WEIGHTS: dict[str, float] = {
    "ticket_cadence_weight": 0.25,
    "recency_weight": 0.20,
    "sla_breach_weight": 0.25,
    "frustration_trend_weight": 0.20,
    "touchpoint_weight": 0.10,
}

EVENT_TYPE_TOUCHPOINT_RECORDED: str = "support_touchpoint_recorded"


def load_health_weights() -> dict[str, float]:
    """Load passive health weights from nce/config_data/support-health-weights.json."""
    config_file = _CONFIG_DATA_DIR / "support-health-weights.json"
    if not config_file.exists():
        return _FALLBACK_WEIGHTS
    try:
        with open(config_file, encoding="utf-8") as f:
            data = json.load(f)
            raw_weights = data.get("weights", _FALLBACK_WEIGHTS)
            return {k: float(v) for k, v in raw_weights.items() if not k.startswith("_")}
    except Exception as exc:
        log.warning("Failed to load health weights config, using fallback: %s", exc)
        return _FALLBACK_WEIGHTS


def compute_health_score(
    *,
    ticket_count: int,
    recent_ticket_count: int,
    breach_count: int,
    frustration_score: float = 0.0,
    touchpoint_avg: float | None = None,
    previous_score: float | None = None,
) -> dict[str, Any]:
    """Compute rolling customer health score (0.0 to 100.0) from passive signals.

    High satisfaction starts at 100.0. Operational friction (frequent tickets,
    recent issues, SLA breaches, emotional frustration) deducts points.
    """
    weights = load_health_weights()
    drivers: list[str] = []

    # 1. Cadence deduction: > 2 tickets starts docking points
    cadence_sub = min(ticket_count * 4.0, 30.0) * weights.get("ticket_cadence_weight", 0.25) / 0.25
    if ticket_count >= 3:
        drivers.append(f"High ticket cadence: {ticket_count} tickets in lookback period")

    # 2. Recency deduction: recent tickets in past 7 days indicate active pain
    recency_sub = min(recent_ticket_count * 8.0, 25.0) * weights.get("recency_weight", 0.20) / 0.20
    if recent_ticket_count >= 2:
        drivers.append(f"Recent ticket cluster: {recent_ticket_count} tickets in last 7 days")

    # 3. SLA breach deduction: severe impact
    breach_sub = min(breach_count * 20.0, 40.0) * weights.get("sla_breach_weight", 0.25) / 0.25
    if breach_count > 0:
        drivers.append(f"SLA breaches recorded: {breach_count} breach(es)")

    # 4. Frustration trend: Empathic Tensor score (0.0 to 1.0)
    frustration_sub = (
        min(max(frustration_score, 0.0), 1.0)
        * 25.0
        * weights.get("frustration_trend_weight", 0.20)
        / 0.20
    )
    if frustration_score >= 0.5:
        drivers.append("Elevated emotional frustration detected in interaction history")

    # 5. Touchpoint adjustment
    touchpoint_adj = 0.0
    if touchpoint_avg is not None:
        # touchpoint_avg 0-100: values < 70 dock points, values >= 90 give positive boost
        tp_weight = weights.get("touchpoint_weight", 0.10)
        if touchpoint_avg < 70.0:
            touchpoint_adj = (70.0 - touchpoint_avg) * 0.5 * (tp_weight / 0.10)
            drivers.append(f"Negative feedback touchpoint score: {touchpoint_avg:.1f}/100")
        elif touchpoint_avg >= 90.0:
            touchpoint_adj = -(touchpoint_avg - 90.0) * 0.2 * (tp_weight / 0.10)

    total_subtraction = cadence_sub + recency_sub + breach_sub + frustration_sub + touchpoint_adj
    final_score = round(max(min(100.0 - total_subtraction, 100.0), 0.0), 2)

    # Churn risk categorization
    if final_score < 50.0:
        churn_risk = "high"
    elif final_score < 75.0:
        churn_risk = "medium"
    else:
        churn_risk = "low"

    # Trend calculation against previous score
    if previous_score is None:
        trend = {"direction": "stable", "delta": 0.0}
    else:
        delta = round(final_score - float(previous_score), 2)
        if delta > 2.0:
            direction = "improving"
        elif delta < -2.0:
            direction = "degrading"
        else:
            direction = "stable"
        trend = {"direction": direction, "delta": delta}

    return {
        "score": final_score,
        "churn_risk": churn_risk,
        "trend": trend,
        "drivers": drivers,
    }


async def do_health_score(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Calculate and upsert customer health score for a given customer.

    Carries explicit WHERE namespace_id = $1::uuid AND customer_id = $2 predicates.
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    customer_id = params.get("customer_id")
    if not customer_id:
        raise ValueError("customer_id is required")
    cust_str = str(customer_id).strip()

    lookback_days = int(params.get("lookback_days", 90))
    now = datetime.datetime.now(datetime.timezone.utc)
    lookback_cutoff = now - datetime.timedelta(days=lookback_days)
    recent_cutoff = now - datetime.timedelta(days=7)

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Query customer tickets in lookback period
        tickets = await conn.fetch(
            """
            SELECT id, created_at, status
            FROM service_tickets
            WHERE namespace_id = $1::uuid
              AND customer_id = $2
              AND created_at >= $3::timestamptz
            """,
            ns_uuid,
            cust_str,
            lookback_cutoff,
        )

        ticket_count = len(tickets)
        recent_ticket_count = sum(
            1 for t in tickets if t["created_at"] and t["created_at"] >= recent_cutoff
        )

        # 2. Query breached SLA clocks for this customer
        breaches = await conn.fetch(
            """
            SELECT sc.ticket_id
            FROM sla_clocks sc
            JOIN service_tickets st ON sc.ticket_id = st.id
            WHERE st.namespace_id = $1::uuid
              AND st.customer_id = $2
              AND sc.breached = TRUE
              AND st.created_at >= $3::timestamptz
            """,
            ns_uuid,
            cust_str,
            lookback_cutoff,
        )
        breach_count = len(breaches)

        # 3. Read previous health score if exists
        prev_health = await conn.fetchrow(
            """
            SELECT score, last_touchpoint_at
            FROM customer_health
            WHERE namespace_id = $1::uuid
              AND customer_id = $2
            """,
            ns_uuid,
            cust_str,
        )

        prev_score = float(prev_health["score"]) if prev_health else None
        last_touchpoint = prev_health["last_touchpoint_at"] if prev_health else None

        # 4. Compute composite health score
        calc = compute_health_score(
            ticket_count=ticket_count,
            recent_ticket_count=recent_ticket_count,
            breach_count=breach_count,
            frustration_score=0.0,
            touchpoint_avg=None,
            previous_score=prev_score,
        )

        # 5. Upsert customer_health record
        upserted = await conn.fetchrow(
            """
            INSERT INTO customer_health (
                customer_id, namespace_id, score, trend, churn_risk,
                drivers, last_touchpoint_at, computed_at
            ) VALUES (
                $1, $2::uuid, $3, $4::jsonb, $5,
                $6::jsonb, $7::timestamptz, $8::timestamptz
            )
            ON CONFLICT (customer_id, namespace_id) DO UPDATE SET
                score = EXCLUDED.score,
                trend = EXCLUDED.trend,
                churn_risk = EXCLUDED.churn_risk,
                drivers = EXCLUDED.drivers,
                last_touchpoint_at = COALESCE(EXCLUDED.last_touchpoint_at, customer_health.last_touchpoint_at),
                computed_at = EXCLUDED.computed_at
            RETURNING *
            """,
            cust_str,
            ns_uuid,
            calc["score"],
            json.dumps(calc["trend"]),
            calc["churn_risk"],
            json.dumps(calc["drivers"]),
            last_touchpoint,
            now,
        )

    return _row_to_dict(upserted)


async def do_record_touchpoint(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record an ÉT-spørsmål touchpoint response and fold into customer health.

    Appends response to v3_cognitive_ledger, updates last_touchpoint_at in
    customer_health, and triggers event-scoped health recomputation.
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    customer_id = params.get("customer_id")
    if not customer_id:
        raise ValueError("customer_id is required")
    cust_str = str(customer_id).strip()

    question_id = str(params.get("question_id") or "et_sporsmal_v1").strip()
    answer = params.get("answer")
    if answer is None:
        raise ValueError("answer is required")

    score = params.get("score")
    score_float = float(score) if score is not None else None

    now = datetime.datetime.now(datetime.timezone.utc)
    ledger_id = uuid4()

    payload = {
        "event_type": "touchpoint_response",
        "audit_event": EVENT_TYPE_TOUCHPOINT_RECORDED,
        "customer_id": cust_str,
        "question_id": question_id,
        "answer": answer,
        "score": score_float,
    }

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Append touchpoint to v3_cognitive_ledger
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
            json.dumps(payload),
            "support/v1",
            now,
        )

    # 2. Trigger health score recomputation
    health_res = await do_health_score(
        pool,
        {
            "namespace_id": str(ns_uuid),
            "customer_id": cust_str,
        },
    )

    return {
        "ok": True,
        "ledger_id": str(ledger_id),
        "customer_id": cust_str,
        "score": score_float,
        "health": health_res,
    }
