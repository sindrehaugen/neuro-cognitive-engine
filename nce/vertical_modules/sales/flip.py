"""
nce/vertical_modules/sales/flip.py
===================================
The stand-alone flip logic for the Sales vertical module.
Enforces the flip-gate: functions can only flip both→nce when the sales
divergence log is clean over the parity window.
Includes the stalled-deal Watcher and the Morning-brief Sales slice.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.sales.flip")


async def do_flip_function(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Flip a Sales function's source mode to 'nce'.

    Blocked if there are unresolved divergence logs in the configured parity window.

    Params:
      - namespace_id: str | UUID (required)
      - function: str (required, e.g., "read_customers")
      - window_days: int (optional, default 7)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    function_name = params.get("function")
    if not function_name:
        raise ValueError("function is required")

    window_days = int(params.get("window_days", 7))
    threshold = datetime.timedelta(days=window_days)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Check divergence log for 'sales' engine
        divergences = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM divergence_log
            WHERE namespace_id = $1::uuid
              AND engine = 'sales'
              AND detected_at >= NOW() - $2::interval
            """,
            ns_uuid,
            threshold,
        )

        if divergences > 0:
            return {
                "ok": False,
                "reason": f"Refused: {divergences} divergence(s) detected in the last {window_days} days.",
                "divergences_count": divergences,
            }

        # Update source_mode_config to 'nce'
        await conn.execute(
            """
            INSERT INTO source_mode_config (namespace_id, engine, function, mode, updated_at)
            VALUES ($1::uuid, 'sales', $2, 'nce', NOW())
            ON CONFLICT (namespace_id, engine, function) DO UPDATE
                SET mode = 'nce',
                    updated_at = NOW()
            """,
            ns_uuid,
            function_name,
        )

    return {
        "ok": True,
        "function": function_name,
        "mode": "nce",
    }


async def do_stalled_deal_watcher(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Identify stalled opportunities/deals that have no updates for a given period.

    Params:
      - namespace_id: str | UUID (required)
      - slip_days: int (optional, default 30)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    slip_days = int(params.get("slip_days", 30))
    threshold = datetime.timedelta(days=slip_days)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT source_id, name, updated_at, source_json
            FROM sales_read_model
            WHERE namespace_id = $1::uuid
              AND entity = 'opportunities'
              AND is_deleted = false
              AND updated_at < NOW() - $2::interval
            """,
            ns_uuid,
            threshold,
        )

        stalled_deals = []
        for row in rows:
            source_json = row["source_json"]
            if isinstance(source_json, str):
                source_json = json.loads(source_json)

            statecode = source_json.get("statecode")
            if statecode is not None and str(statecode) != "0":
                continue

            stalled_deals.append(
                {
                    "deal_id": row["source_id"],
                    "name": row["name"],
                    "stalled_since": row["updated_at"].isoformat() if row["updated_at"] else None,
                    "payload": source_json,
                }
            )

    return {
        "ok": True,
        "stalled_deals_count": len(stalled_deals),
        "stalled_deals": stalled_deals,
    }


async def do_morning_brief_slice(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Expose pipeline value, at-risk deals, and won-this-period for the Morning Brief.

    Params:
      - namespace_id: str | UUID (required)
      - period_days: int (optional, default 7)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    period_days = int(params.get("period_days", 7))
    period_delta = datetime.timedelta(days=period_days)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT source_id, name, updated_at, source_json
            FROM sales_read_model
            WHERE namespace_id = $1::uuid
              AND entity = 'opportunities'
              AND is_deleted = false
            """,
            ns_uuid,
        )

        pipeline_value = 0.0
        at_risk_count = 0
        won_value = 0.0
        won_count = 0

        now = datetime.datetime.now(datetime.timezone.utc)

        for row in rows:
            source_json = row["source_json"]
            if isinstance(source_json, str):
                source_json = json.loads(source_json)

            statecode = source_json.get("statecode")
            # Estimated value for open pipeline
            est_value_raw = source_json.get("estimatedvalue")
            est_value = float(est_value_raw) if est_value_raw is not None else 0.0

            # Actual value for won
            act_value_raw = source_json.get("actualvalue")
            act_value = float(act_value_raw) if act_value_raw is not None else est_value

            # Check state (0 = Open, 1 = Won, 2 = Lost)
            state_str = str(statecode) if statecode is not None else "0"

            if state_str == "0":
                pipeline_value += est_value
                # If stalled (no updates for 14 days), count as at-risk
                updated_at = row["updated_at"]
                if updated_at:
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
                    if now - updated_at > datetime.timedelta(days=14):
                        at_risk_count += 1

            elif state_str == "1":
                updated_at = row["updated_at"]
                if updated_at:
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
                    if now - updated_at <= period_delta:
                        won_value += act_value
                        won_count += 1

    return {
        "ok": True,
        "pipeline_value": pipeline_value,
        "at_risk_deals_count": at_risk_count,
        "won_value_this_period": won_value,
        "won_count_this_period": won_count,
    }
