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
from nce.source_mode.divergence import alert_threshold

log = logging.getLogger("nce.vertical_modules.sales.flip")


async def do_read_sales_divergence(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Read Sales divergence log entries and evaluate the parity window.

    Evaluates discrepancies logged during C5 'both' mode synchronization
    between D365 and NCE. Returns parity metrics, whether a cutover flip to
    'nce' mode is blocked, and paginated divergence log items.

    Params:
      - namespace_id: str | UUID (required)
      - window_days: int | float (optional, default 7.0)
      - window_seconds: int | float (optional, overrides window_days)
      - entity: str (optional, e.g. "accounts", "opportunities")
      - limit: int (optional, default 100, max 500)
      - offset: int (optional, default 0)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    if "window_seconds" in params and params["window_seconds"] is not None:
        win_seconds = float(params["window_seconds"])
    else:
        win_days = float(params.get("window_days", 7.0))
        win_seconds = win_days * 86400.0

    threshold_delta = datetime.timedelta(seconds=win_seconds)
    threshold_mat = alert_threshold()

    limit = min(max(1, int(params.get("limit", 100))), 500)
    offset = max(0, int(params.get("offset", 0)))
    entity_filter = str(params["entity"]).strip() if params.get("entity") else None

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        if entity_filter:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS total_count,
                       COUNT(*) FILTER (WHERE materiality > $3)::int AS material_count
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = 'sales'
                   AND detected_at >= NOW() - $2::interval
                   AND entity = $4
                """,
                ns_uuid,
                threshold_delta,
                threshold_mat,
                entity_filter,
            )
            rows = await conn.fetch(
                """
                SELECT id, entity, field, nce_value, ext_value, materiality, detected_at
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = 'sales'
                   AND detected_at >= NOW() - $2::interval
                   AND entity = $3
                 ORDER BY detected_at DESC, id DESC
                 LIMIT $4 OFFSET $5
                """,
                ns_uuid,
                threshold_delta,
                entity_filter,
                limit,
                offset,
            )
        else:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS total_count,
                       COUNT(*) FILTER (WHERE materiality > $3)::int AS material_count
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = 'sales'
                   AND detected_at >= NOW() - $2::interval
                """,
                ns_uuid,
                threshold_delta,
                threshold_mat,
            )
            rows = await conn.fetch(
                """
                SELECT id, entity, field, nce_value, ext_value, materiality, detected_at
                  FROM divergence_log
                 WHERE namespace_id = $1::uuid
                   AND engine = 'sales'
                   AND detected_at >= NOW() - $2::interval
                 ORDER BY detected_at DESC, id DESC
                 LIMIT $3 OFFSET $4
                """,
                ns_uuid,
                threshold_delta,
                limit,
                offset,
            )

        total_count = (
            int(count_row["total_count"])
            if count_row and count_row["total_count"] is not None
            else 0
        )
        material_count = (
            int(count_row["material_count"])
            if count_row and count_row["material_count"] is not None
            else 0
        )

        items = []
        for r in rows:
            mat_val = float(r["materiality"]) if r["materiality"] is not None else 0.0
            items.append(
                {
                    "id": r["id"],
                    "entity": r["entity"],
                    "field": r["field"],
                    "nce_value": r["nce_value"],
                    "ext_value": r["ext_value"],
                    "materiality": mat_val,
                    "is_material": mat_val > threshold_mat,
                    "detected_at": (r["detected_at"].isoformat() if r["detected_at"] else None),
                }
            )

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "engine": "sales",
        "window_seconds": win_seconds,
        "clean": total_count == 0,
        "flip_blocked": total_count > 0,
        "divergences_count": total_count,
        "material_divergences_count": material_count,
        "alert_threshold": threshold_mat,
        "items": items,
    }


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
