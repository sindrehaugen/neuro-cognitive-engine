"""
nce/vertical_modules/vendors/frontier.py
=========================================
Reliability radar and scorecard weight calibration (Batch 104).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors.scorecard import load_scorecard_weights
from nce.vertical_modules.vendors.tiers import do_get_tier_status

log = logging.getLogger("nce.vertical_modules.vendors.frontier")

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"


def parse_json(val: Any) -> Any:
    """Parse JSON field values helper (handles both dict and string representations)."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return val or {}


async def do_reliability_radar(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Analyze supplier-risk and contractor-burnout signals.

    Advisor only.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    supplier_risks: list[dict[str, Any]] = []
    contractor_burnouts: list[dict[str, Any]] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # --- 1. Supplier / Vendor Risk ---
        # Fetch all vendor scorecards
        scorecard_rows = await conn.fetch(
            """
            SELECT vendor_id, (raw->>'composite_score')::numeric AS composite_score, defect_rma_rate, on_time_pct, current_tier, ytd_progress
            FROM vendor_scorecards
            WHERE namespace_id = $1::uuid
            """,
            ns_uuid,
        )

        for row in scorecard_rows:
            vendor_id = row["vendor_id"]
            composite_score = (
                float(row["composite_score"]) if row["composite_score"] is not None else None
            )
            defect_rma_rate = (
                float(row["defect_rma_rate"]) if row["defect_rma_rate"] is not None else None
            )
            float(row["on_time_pct"]) if row["on_time_pct"] is not None else None

            reasons: list[str] = []
            risk_level = None

            # High defect rate or low composite score
            if composite_score is not None and composite_score < 70.0:
                risk_level = "high"
                reasons.append(f"Low composite score ({composite_score:.1f} < 70.0)")
            elif defect_rma_rate is not None and defect_rma_rate > 10.0:
                risk_level = "high"
                reasons.append(f"High defect RMA rate ({defect_rma_rate:.1f}% > 10.0%)")

            # Check degradation trend from v3_cognitive_ledger
            # Query recent outcomes (PO match results / match decisions)
            outcome_rows = await conn.fetch(
                """
                SELECT tlx_scores
                FROM v3_cognitive_ledger
                WHERE namespace_id = $1::uuid
                  AND (tlx_scores->>'supplier_id' = $2 OR tlx_scores->>'vendor_id' = $2)
                  AND tlx_scores->>'event_type' IN ('match_decision', 'procurement_match')
                ORDER BY created_at DESC
                """,
                ns_uuid,
                vendor_id,
            )

            outcomes: list[dict[str, Any]] = []
            for o_row in outcome_rows:
                scores = parse_json(o_row["tlx_scores"])
                if scores:
                    outcomes.append(scores)

            if len(outcomes) >= 4:
                half = len(outcomes) // 2
                recent = outcomes[:half]
                prior = outcomes[half:]

                # Check on_time trend
                recent_ot = [o.get("on_time") for o in recent if o.get("on_time") is not None]
                prior_ot = [o.get("on_time") for o in prior if o.get("on_time") is not None]
                if recent_ot and prior_ot:
                    recent_ot_pct = (
                        sum(1 for x in recent_ot if x is True or x == 1) / len(recent_ot)
                    ) * 100.0
                    prior_ot_pct = (
                        sum(1 for x in prior_ot if x is True or x == 1) / len(prior_ot)
                    ) * 100.0
                    if recent_ot_pct < prior_ot_pct - 5.0:  # degraded by more than 5%
                        risk_level = risk_level or "medium"
                        reasons.append(
                            f"On-time rate degrading ({prior_ot_pct:.1f}% -> {recent_ot_pct:.1f}%)"
                        )

                # Check defect_rma trend
                recent_df = [o.get("defect_rma") for o in recent if o.get("defect_rma") is not None]
                prior_df = [o.get("defect_rma") for o in prior if o.get("defect_rma") is not None]
                if recent_df and prior_df:
                    recent_df_rate = (
                        sum(1 for x in recent_df if x is True or x == 1) / len(recent_df)
                    ) * 100.0
                    prior_df_rate = (
                        sum(1 for x in prior_df if x is True or x == 1) / len(prior_df)
                    ) * 100.0
                    if recent_df_rate > prior_df_rate + 5.0:  # degraded by more than 5%
                        risk_level = risk_level or "medium"
                        reasons.append(
                            f"Defect rate increasing ({prior_df_rate:.1f}% -> {recent_df_rate:.1f}%)"
                        )

            # Check kickback tier at risk ("days-left vs ytd-pace" race)
            try:
                tier_status = await do_get_tier_status(
                    engine, {"namespace_id": ns_uuid, "vendor_id": vendor_id}
                )
                ytd_vol = float(tier_status.get("ytd_volume", 0.0))
                next_thresh = tier_status.get("next_tier_threshold")
                days_left = tier_status.get("days_left", 365)
                if next_thresh is not None and days_left > 0:
                    next_thresh = float(next_thresh)
                    days_elapsed = max(1, 365 - days_left)
                    ytd_run_rate = ytd_vol / days_elapsed
                    needed_run_rate = (next_thresh - ytd_vol) / days_left
                    if ytd_run_rate < needed_run_rate:
                        risk_level = risk_level or "medium"
                        reasons.append(
                            f"Kickback tier at risk: YTD pace ({ytd_run_rate:.1f}/day) below target ({needed_run_rate:.1f}/day)"
                        )
            except Exception as e:
                log.warning("Could not calculate tier-at-risk for vendor %s: %s", vendor_id, e)

            if risk_level:
                supplier_risks.append(
                    {
                        "vendor_id": vendor_id,
                        "risk_level": risk_level,
                        "reasons": reasons,
                        "composite_score": composite_score,
                    }
                )

        # --- 2. Contractor Burnout ---
        # Fetch all contractor profiles
        contractor_rows = await conn.fetch(
            """
            SELECT contractor_id, performance_score
            FROM contractor_profiles
            WHERE namespace_id = $1::uuid
            """,
            ns_uuid,
        )

        # Get active loads
        load_rows = await conn.fetch(
            """
            SELECT object_label AS contractor_id, COUNT(*) AS active_load
            FROM kg_edges
            WHERE predicate = 'assigned_to' AND namespace_id = $1::uuid
            GROUP BY object_label
            """,
            ns_uuid,
        )
        load_map = {r["contractor_id"]: int(r["active_load"]) for r in load_rows}

        for row in contractor_rows:
            contractor_id = row["contractor_id"]
            performance_score = (
                float(row["performance_score"]) if row["performance_score"] is not None else None
            )
            active_load = load_map.get(contractor_id, 0)

            reasons = []
            risk_level = None

            # High workload burnout signal
            burnout_threshold = 3
            if active_load > burnout_threshold:
                risk_level = "high"
                reasons.append(
                    f"Excessive active load ({active_load} assignments > {burnout_threshold})"
                )

            # Check performance rating degradation trend
            rating_rows = await conn.fetch(
                """
                SELECT tlx_scores
                FROM v3_cognitive_ledger
                WHERE namespace_id = $1::uuid
                  AND tlx_scores->>'contractor_id' = $2
                  AND tlx_scores->>'event_type' = 'work_order_rating'
                  AND tlx_scores->'rating' IS NOT NULL
                ORDER BY created_at DESC
                """,
                ns_uuid,
                contractor_id,
            )

            ratings = []
            for r_row in rating_rows:
                scores = parse_json(r_row["tlx_scores"])
                rating_val = scores.get("rating")
                if rating_val is not None:
                    ratings.append(float(rating_val))

            if len(ratings) >= 4:
                half = len(ratings) // 2
                recent_ratings = ratings[:half]
                prior_ratings = ratings[half:]

                recent_avg = sum(recent_ratings) / len(recent_ratings)
                prior_avg = sum(prior_ratings) / len(prior_ratings)

                if recent_avg < prior_avg - 0.5:  # dropped by more than 0.5 out of 5
                    risk_level = risk_level or "medium"
                    reasons.append(
                        f"Performance rating degrading ({prior_avg:.1f} -> {recent_avg:.1f})"
                    )

            if risk_level:
                contractor_burnouts.append(
                    {
                        "contractor_id": contractor_id,
                        "risk_level": risk_level,
                        "active_load": active_load,
                        "reasons": reasons,
                        "performance_score": performance_score,
                    }
                )

    return {
        "ok": True,
        "supplier_risk": supplier_risks,
        "contractor_burnout": contractor_burnouts,
    }


async def do_calibrate_weights(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Recalibrate vendor scorecard weights dynamically based on ledger outcomes.

    Counts late deliveries vs defects to adjust relative weights.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    # 1. Fetch current weights
    current_weights = load_scorecard_weights()

    # 2. Count late deliveries and defects across the namespace
    late_count = 0
    defect_count = 0

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT tlx_scores
            FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND tlx_scores->>'event_type' IN ('match_decision', 'procurement_match')
            """,
            ns_uuid,
        )

        for r in rows:
            scores = parse_json(r["tlx_scores"])
            if not scores:
                continue

            on_time = scores.get("on_time")
            if on_time is False or on_time == 0:
                late_count += 1

            defect = scores.get("defect_rma")
            if defect is True or defect == 1:
                defect_count += 1

    total_issues = late_count + defect_count
    calibrated_weights = current_weights.copy()

    # 3. Redistribute the 0.7 combined weight of on_time + defect based on issue frequency
    if total_issues > 0:
        on_time_ratio = late_count / total_issues
        defect_ratio = defect_count / total_issues

        # Target weight redistribution (base weight allocation sum = 0.7)
        target_ot = 0.7 * on_time_ratio
        target_df = 0.7 * defect_ratio

        # Clamp between 0.10 and 0.60 to avoid zeroing out any metric
        target_ot = max(0.10, min(0.60, target_ot))
        target_df = max(0.10, min(0.60, target_df))

        # Rest is shared by substitution (0.1) and reliability (0.2)
        sub_w = 0.1
        rel_w = 0.2

        total_sum = target_ot + target_df + sub_w + rel_w
        if total_sum > 0:
            # Normalize to exactly 1.0
            calibrated_weights["on_time_weight"] = round(target_ot / total_sum, 2)
            calibrated_weights["defect_rma_weight"] = round(target_df / total_sum, 2)
            calibrated_weights["substitution_weight"] = round(sub_w / total_sum, 2)
            calibrated_weights["reliability_weight"] = round(rel_w / total_sum, 2)

            # Ensure strict sum to 1.0 due to rounding
            total_calc = (
                calibrated_weights["on_time_weight"]
                + calibrated_weights["defect_rma_weight"]
                + calibrated_weights["substitution_weight"]
                + calibrated_weights["reliability_weight"]
            )
            diff = round(1.0 - total_calc, 2)
            if diff != 0.0:
                highest_key = max(calibrated_weights, key=lambda k: calibrated_weights[k])
                calibrated_weights[highest_key] = round(calibrated_weights[highest_key] + diff, 2)

        # 4. Overwrite vendor-scorecard-weights.json config file
        weights_path = _CONFIG_DATA_DIR / "vendor-scorecard-weights.json"
        if weights_path.exists():
            try:
                with open(weights_path, encoding="utf-8") as f:
                    orig_json = json.load(f)

                comment = orig_json.get("_comment")
                write_json = {}
                if comment:
                    write_json["_comment"] = comment
                write_json.update(calibrated_weights)

                with open(weights_path, "w", encoding="utf-8") as f:
                    json.dump(write_json, f, indent=2, ensure_ascii=False)
            except Exception as e:
                log.error("Failed to write calibrated weights: %s", e)

    return {
        "ok": True,
        "calibrated_weights": calibrated_weights,
        "total_issues": total_issues,
        "late_count": late_count,
        "defect_count": defect_count,
    }
