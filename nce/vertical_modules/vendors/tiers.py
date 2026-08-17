"""
nce/vertical_modules/vendors/tiers.py
======================================
Vendor kickback tiers and outcomes module logic (Batch 097).
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Any
from uuid import UUID

from bson import ObjectId

from nce.db_utils import scoped_mongo_session, scoped_pg_session

log = logging.getLogger("nce.vertical_modules.vendors.tiers")


def strip_tier_details(vendor_data: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive vendor tier details from customer-facing / external projections.

    Parameters
    ----------
    vendor_data:
        The vendor projection dictionary.

    Returns
    -------
    dict:
        A redacted copy of the vendor dictionary with tier details removed.
    """
    res = dict(vendor_data)

    # Strip from root level
    res.pop("current_tier", None)
    res.pop("ytd_progress", None)
    res.pop("ytd_volume", None)
    res.pop("next_tier_threshold", None)
    res.pop("days_left", None)

    # Strip from nested scorecard dictionary if present
    if "scorecard" in res and isinstance(res["scorecard"], dict):
        scorecard = dict(res["scorecard"])
        scorecard.pop("current_tier", None)
        scorecard.pop("ytd_progress", None)
        res["scorecard"] = scorecard

    return res


async def do_get_tier_status(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Get the current kickback tier status, YTD volume, progress, and days left for a vendor.

    Parameters
    ----------
    engine:
        The NCE engine stub.
    params:
        dict containing:
            namespace_id (str | UUID): active namespace UUID
            vendor_id (str): vendor label, ID, or source ID
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    vendor_id = params.get("vendor_id") or params.get("supplier_id")
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id_str = str(vendor_id).strip()

    # 1. Fetch vendor label and identity from kg_nodes
    vendor_label = None
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT label, payload_ref 
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

    # 2. Query agreements by reference from Agreements:
    # Look for VENDOR -[under]-> AGREEMENT edge in kg_edges
    agreement_label = None
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        edge = await conn.fetchrow(
            """
            SELECT object_label 
            FROM kg_edges 
            WHERE subject_label = $1 
              AND predicate = 'under' 
              AND namespace_id = $2
            """,
            vendor_label,
            ns_uuid,
        )
        if edge:
            agreement_label = edge["object_label"]

    # 3. Retrieve kickback tiers terms from the agreement node
    kickback_tiers = None
    if agreement_label:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            ag_node = await conn.fetchrow(
                """
                SELECT payload_ref 
                FROM kg_nodes 
                WHERE label = $1 
                  AND namespace_id = $2
                """,
                agreement_label,
                ns_uuid,
            )
            if ag_node and ag_node["payload_ref"] and engine.mongo_client:
                try:
                    async with scoped_mongo_session(engine.mongo_client, ns_uuid) as db:
                        doc = await db.episodes.find_one({"_id": ObjectId(ag_node["payload_ref"])})
                        if doc:
                            # Support both snake_case and camelCase
                            kickback_tiers = doc.get("kickback_tiers") or doc.get("kickbackTiers")
                except Exception as e:
                    log.warning(
                        "Failed to fetch agreement MongoDB payload for ref %s: %s",
                        ag_node["payload_ref"],
                        e,
                    )

    # Fallback to default tiers if not defined in the agreement
    if not kickback_tiers:
        kickback_tiers = [
            {"tier": "Bronze", "threshold": 10000.0, "pct": 1.0},
            {"tier": "Silver", "threshold": 50000.0, "pct": 2.0},
            {"tier": "Gold", "threshold": 100000.0, "pct": 3.0},
            {"tier": "Platinum", "threshold": 250000.0, "pct": 5.0},
        ]

    # Sort tiers by threshold to ensure correct ordering
    kickback_tiers = sorted(kickback_tiers, key=lambda x: float(x["threshold"]))

    # 4. Calculate YTD volume (real spend) for this vendor
    # We query match outcomes / match decisions from v3_cognitive_ledger in the current year
    today = datetime.date.today()
    start_of_year = datetime.datetime(today.year, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)

    ytd_volume = 0.0
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT tlx_scores 
            FROM v3_cognitive_ledger 
            WHERE namespace_id = $1::uuid
              AND (tlx_scores->>'supplier_id' = $2 OR tlx_scores->>'vendor_id' = $2)
              AND created_at >= $3::timestamptz
            """,
            ns_uuid,
            vendor_label,
            start_of_year,
        )
        for r in rows:
            scores = r["tlx_scores"]
            if isinstance(scores, str):
                scores = json.loads(scores)
            if scores:
                # Try to get volume/amount, default to score if no amount is present
                vol = scores.get("amount") or scores.get("volume") or scores.get("value")
                if vol is not None:
                    ytd_volume += float(vol)
                elif (
                    scores.get("event_type") in ("match_decision", "procurement_match")
                    and scores.get("score") is not None
                ):
                    # Fallback to score
                    ytd_volume += float(scores.get("score"))

    # 5. Determine current tier and next tier threshold
    current_tier = "Base"
    next_tier_threshold = None

    for tier_info in kickback_tiers:
        thresh = float(tier_info["threshold"])
        if ytd_volume >= thresh:
            current_tier = tier_info["tier"]
        else:
            next_tier_threshold = thresh
            break

    # Calculate progress towards the next tier threshold
    if next_tier_threshold is None:
        # Already at highest tier
        ytd_progress = 1.0
    else:
        # Find current tier threshold (lower bound)
        current_threshold = 0.0
        for tier_info in kickback_tiers:
            thresh = float(tier_info["threshold"])
            if thresh < next_tier_threshold:
                current_threshold = thresh
            else:
                break
        denom = next_tier_threshold - current_threshold
        if denom > 0:
            ytd_progress = float((ytd_volume - current_threshold) / denom)
        else:
            ytd_progress = 0.0
        ytd_progress = max(0.0, min(1.0, ytd_progress))

    # 6. Calculate days left in the year
    end_of_year = datetime.date(today.year, 12, 31)
    days_left = (end_of_year - today).days

    # 7. Persist current_tier and ytd_progress to vendor_scorecards table if it exists
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        scorecard_row = await conn.fetchrow(
            "SELECT 1 FROM vendor_scorecards WHERE vendor_id = $1 AND namespace_id = $2",
            vendor_label,
            ns_uuid,
        )
        if scorecard_row:
            await conn.execute(
                """
                UPDATE vendor_scorecards
                SET current_tier = $1,
                    ytd_progress = $2,
                    computed_at = NOW()
                WHERE vendor_id = $3
                  AND namespace_id = $4
                """,
                current_tier,
                ytd_progress,
                vendor_label,
                ns_uuid,
            )
        else:
            await conn.execute(
                """
                INSERT INTO vendor_scorecards (
                    vendor_id, namespace_id, current_tier, ytd_progress, computed_at
                ) VALUES ($1, $2, $3, $4, NOW())
                """,
                vendor_label,
                ns_uuid,
                current_tier,
                ytd_progress,
            )

    return {
        "vendor_id": vendor_label,
        "current_tier": current_tier,
        "ytd_volume": ytd_volume,
        "next_tier_threshold": next_tier_threshold,
        "ytd_progress": ytd_progress,
        "days_left": days_left,
    }


async def do_record_outcome(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record a procurement match outcome or contractor rating.

    Appends an outcome event to v3_cognitive_ledger.

    Parameters
    ----------
    engine:
        The NCE engine stub.
    params:
        dict containing:
            namespace_id (str | UUID): active namespace UUID
            event_type (str): event type identifier (e.g. 'match_decision', 'work_order_rating')
            vendor_id (str, optional): vendor/supplier label or ID
            supplier_id (str, optional): supplier/vendor label or ID (alias)
            contractor_id (str, optional): contractor label or ID
            decision (str, optional): 'accept' or 'override'
            score (float, optional): confidence/match score or rating
            rating (float, optional): contractor rating
            amount (float, optional): PO value/volume
            work_order_id (str, optional): work order ID
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    event_type = params.get("event_type")
    if not event_type:
        raise ValueError("event_type is required")

    # Build the payload for tlx_scores
    payload: dict[str, Any] = {
        "event_type": event_type,
    }

    # Optional fields
    vendor_id = params.get("vendor_id") or params.get("supplier_id")
    if vendor_id:
        payload["vendor_id"] = str(vendor_id).strip()
        payload["supplier_id"] = str(vendor_id).strip()

    contractor_id = params.get("contractor_id")
    if contractor_id:
        payload["contractor_id"] = str(contractor_id).strip()

    for k in ("decision", "score", "rating", "amount", "volume", "value", "work_order_id"):
        if params.get(k) is not None:
            payload[k] = params.get(k)

    # Insert into v3_cognitive_ledger
    ledger_id = uuid.uuid4()
    _ZERO_TENSOR = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                id, namespace_id, memory_id,
                empathic_tensor, tlx_scores, vad_scores, model_version
            ) VALUES (
                $1::uuid, $2::uuid, NULL,
                $3::float[], $4::jsonb, $5::jsonb, $6
            )
            """,
            str(ledger_id),
            str(ns_uuid),
            _ZERO_TENSOR,
            json.dumps(payload),
            json.dumps({}),
            "vendors-outcome-1.0",
        )

    return {
        "ok": True,
        "ledger_id": str(ledger_id),
        "event_type": event_type,
    }
