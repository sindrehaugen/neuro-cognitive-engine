"""
nce/vertical_modules/vendors/feed.py
=====================================
Reliability-degradation and tier-at-risk Watchers for Vendors (Batch 098).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors.tiers import do_get_tier_status

log = logging.getLogger("nce.vertical_modules.vendors.feed")


async def do_detect_reliability_degradation(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Detect reliability degradation for a vendor over their outcome events history.

    Splits outcomes in two halves chronologically and checks if recent performance
    trends downwards.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    vendor_id = params.get("vendor_id") or params.get("supplier_id")
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id_str = str(vendor_id).strip()

    # Find the vendor label in kg_nodes to resolve aliases
    vendor_label = None
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT label 
            FROM kg_nodes 
            WHERE (label = $1 OR vendors_source_id = $1 OR id::text = $1)
              AND namespace_id = $2 
              AND entity_type = 'VENDOR'
            """,
            vendor_id_str,
            ns_uuid,
        )
        if row:
            vendor_label = row["label"]

    if not vendor_label:
        raise ValueError(f"Vendor not found: {vendor_id_str}")

    # Fetch outcomes from v3_cognitive_ledger
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT tlx_scores, created_at 
            FROM v3_cognitive_ledger 
            WHERE namespace_id = $1::uuid
              AND (tlx_scores->>'supplier_id' = $2 OR tlx_scores->>'vendor_id' = $2)
              AND (
                tlx_scores->'on_time' IS NOT NULL 
                OR tlx_scores->'defect_rma' IS NOT NULL 
                OR tlx_scores->'reliability' IS NOT NULL
              )
            ORDER BY created_at ASC
            """,
            ns_uuid,
            vendor_label,
        )

    events: list[dict[str, Any]] = []
    for r in rows:
        scores = r["tlx_scores"]
        if isinstance(scores, str):
            try:
                scores = json.loads(scores)
            except Exception:
                continue
        if scores:
            events.append(scores)

    n = len(events)
    min_sample_val = params.get("min_sample")
    if min_sample_val is None:
        min_sample_val = getattr(cfg, "NCE_VENDORS_SCORECARD_MIN_SAMPLE", 4)
    min_sample = int(min_sample_val) if min_sample_val is not None else 4

    if n < min_sample:
        return {
            "vendor_id": vendor_label,
            "degraded": False,
            "reason": f"Insufficient data: got {n} events, min_sample is {min_sample}",
            "sample_n": n,
        }

    # Split into historical (older) and recent (newer) halves
    half = n // 2
    historical_events = events[:half]
    recent_events = events[half:]

    def calc_metrics(evs: list[dict[str, Any]]) -> tuple[float, float]:
        on_time_count = sum(1 for e in evs if e.get("on_time") is True or e.get("on_time") == 1)
        defect_count = sum(
            1 for e in evs if e.get("defect_rma") is True or e.get("defect_rma") == 1
        )
        k = len(evs)
        return (on_time_count / k) * 100.0, (defect_count / k) * 100.0

    hist_on_time, hist_defect = calc_metrics(historical_events)
    recent_on_time, recent_defect = calc_metrics(recent_events)

    on_time_degraded = hist_on_time - recent_on_time
    defect_degraded = recent_defect - hist_defect

    threshold_val = params.get("threshold")
    if threshold_val is None:
        threshold_val = getattr(cfg, "NCE_VENDORS_RELIABILITY_DEGRADE_PCT", 10.0)
    threshold = float(threshold_val) if threshold_val is not None else 10.0

    # Alert condition
    degraded = (on_time_degraded >= threshold) or (defect_degraded >= threshold)

    return {
        "vendor_id": vendor_label,
        "degraded": degraded,
        "on_time_degraded_pct": on_time_degraded,
        "defect_degraded_pct": defect_degraded,
        "historical_on_time_pct": hist_on_time,
        "recent_on_time_pct": recent_on_time,
        "historical_defect_rate": hist_defect,
        "recent_defect_rate": recent_defect,
        "sample_n": n,
        "threshold": threshold,
    }


async def do_check_tier_at_risk(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Check if the vendor is at risk of missing their next kickback tier.

    Compares YTD pace against remaining threshold requirement over remaining days.
    """
    # Fetch current tier status
    status = await do_get_tier_status(engine, params)

    # Check custom params overrides (very useful for unit testing)
    ytd_volume_val = (
        params.get("ytd_volume")
        if params.get("ytd_volume") is not None
        else status.get("ytd_volume")
    )
    ytd_volume = float(ytd_volume_val) if ytd_volume_val is not None else 0.0

    next_tier_threshold_val = (
        params.get("next_tier_threshold")
        if params.get("next_tier_threshold") is not None
        else status.get("next_tier_threshold")
    )
    next_tier_threshold = (
        float(next_tier_threshold_val) if next_tier_threshold_val is not None else None
    )

    days_left_val = (
        params.get("days_left") if params.get("days_left") is not None else status.get("days_left")
    )
    days_left = int(days_left_val) if days_left_val is not None else 0

    if next_tier_threshold is None:
        # Already at highest tier
        return {
            "vendor_id": status["vendor_id"],
            "at_risk": False,
            "reason": "Already at highest tier",
            "current_tier": status["current_tier"],
        }

    total_days = 365
    days_elapsed = max(1, total_days - days_left)
    pace = ytd_volume / days_elapsed

    projected_volume_rest = pace * days_left
    volume_needed = next_tier_threshold - ytd_volume

    at_risk = projected_volume_rest < volume_needed

    return {
        "vendor_id": status["vendor_id"],
        "at_risk": at_risk,
        "current_tier": status["current_tier"],
        "ytd_volume": ytd_volume,
        "next_tier_threshold": next_tier_threshold,
        "days_left": days_left,
        "days_elapsed": days_elapsed,
        "pace_per_day": pace,
        "projected_remaining_volume": projected_volume_rest,
        "needed_remaining_volume": volume_needed,
    }
