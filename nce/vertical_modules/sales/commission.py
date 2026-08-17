"""
nce/vertical_modules/sales/commission.py
=========================================
Reproducible DB-weighted commission calculation, A2A Quote->Design->Procure flow orchestration,
and Product failure-pattern feedback edge generation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.orchestrator import NCEEngine
from nce.vertical_modules.procurement.tco import do_calculate_tco, load_procurement_config
from nce.vertical_modules.product.a2a import enqueue_product_enrichment

# Direct imports from other vertical modules for A2A flow execution
from nce.vertical_modules.system_design.propose import do_propose_design

log = logging.getLogger("nce.vertical_modules.sales.commission")


def load_commission_config() -> dict[str, Any]:
    """Load the versioned commission configuration from config_data."""
    config_path = Path(__file__).parents[2] / "config_data" / "sales-commission.json"
    with open(config_path, encoding="utf-8") as f:
        return cast(dict[str, Any], json.load(f))


def calculate_deal_commission(deal_data: dict[str, Any], config: dict[str, Any]) -> float:
    """Calculate the DB-weighted commission for a single deal based on the commission tiers.

    The commission rate is determined by the overall gross margin % of the deal.
    The rate is then applied to the contribution margin (price - cost) of each line item,
    with service lines weighted differently than hardware lines.
    """
    items = deal_data.get("items") or []
    if not items:
        return 0.0

    total_price = sum(float(item.get("price") or 0.0) for item in items)
    total_cost = sum(float(item.get("cost") or 0.0) for item in items)
    total_profit = total_price - total_cost

    deal_margin = (total_profit / total_price) if total_price > 0.0 else 0.0

    # Locate matching tier
    selected_tier = None
    tiers = config.get("tiers") or []
    for tier in tiers:
        min_m = float(tier.get("min_margin_pct", 0.0))
        max_m = float(tier.get("max_margin_pct", 1.0))
        if min_m <= deal_margin <= max_m:
            selected_tier = tier
            break

    if not selected_tier:
        if tiers:
            selected_tier = tiers[-1]
        else:
            selected_tier = {"hardware_rate": 0.0, "service_rate": 0.0}

    hardware_rate = float(selected_tier.get("hardware_rate", 0.0))
    service_rate = float(selected_tier.get("service_rate", 0.0))

    commission = 0.0
    for item in items:
        price = float(item.get("price") or 0.0)
        cost = float(item.get("cost") or 0.0)
        profit = price - cost

        # Determine the line rate
        item_type = str(item.get("type", "hardware")).lower().strip()
        rate = service_rate if item_type == "service" else hardware_rate
        commission += profit * rate

    return commission


async def do_calculate_commission(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Orchestrate commission calculations from direct input or ledger events.

    Params:
      - namespace_id: str | UUID (required)
      - seller_id: str (optional, filters history by seller)
      - deal_data: dict (optional, direct calculation bypasses ledger)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    config = load_commission_config()

    if "deal_data" in params:
        deal_data = params["deal_data"]
        commission = calculate_deal_commission(deal_data, config)
        return {
            "ok": True,
            "commission": commission,
            "config_version": config.get("version"),
        }

    seller_id = params.get("seller_id")
    commissions = []
    total_commission = 0.0

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT id, event_seq, params, occurred_at
            FROM event_log
            WHERE namespace_id = $1::uuid
              AND event_type = 'sales_ai_decision'
              AND (params->>'decision_type') = 'deal_won'
            ORDER BY event_seq ASC
            """,
            ns_uuid,
        )

        for row in rows:
            event_params = row["params"]
            if isinstance(event_params, str):
                event_params = json.loads(event_params)

            details = event_params.get("details") or {}
            event_seller = details.get("seller_id")

            if seller_id and event_seller != seller_id:
                continue

            comm = calculate_deal_commission(details, config)
            total_commission += comm
            commissions.append(
                {
                    "event_id": str(row["id"]),
                    "event_seq": row["event_seq"],
                    "quote_id": details.get("quote_id"),
                    "seller_id": event_seller,
                    "commission": comm,
                    "created_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
                }
            )

    return {
        "ok": True,
        "seller_id": seller_id,
        "total_commission": total_commission,
        "commissions": commissions,
        "config_version": config.get("version"),
    }


async def do_initiate_quote_flow(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Initiate the multi-hop Agent-to-Agent Quote -> Design -> Procure flow.

    Asks System Design for a BOM, then triggers Product spec enrichment,
    and then calculates the Procurement TCO for the design BOM lines.

    Params:
      - namespace_id: str | UUID (required)
      - query_text: str (required, e.g., the deal description for design proposal)
      - product_id: str (required, the SKU/product identifier for enrichment)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    query_text = params.get("query_text")
    if not query_text:
        raise ValueError("query_text is required")

    product_id = params.get("product_id")
    if not product_id:
        raise ValueError("product_id is required")

    # 1. Ask System Design for a BOM (design proposal)
    sd_proposal = await do_propose_design(
        engine,
        {
            "namespace_id": str(ns_uuid),
            "room_brief": query_text,
            "top_k": 1,
        },
    )

    # 2. Fire-and-forget: ask Product for missing specs/enrichment
    prod_enrichment = enqueue_product_enrichment(
        engine.pg_pool,
        str(ns_uuid),
        product_id,
        {
            "missing_fields": ["specs", "dimensions"],
            "source_watermark": "sales-a2a-trigger",
        },
    )

    # 3. Ask Procurement for TCO calculation on a hypothetical line
    weights, tolerances = load_procurement_config()
    supplier = {"unit_price": 500.0, "quantity": 1}
    bom_line = {"quantity": 2, "unit_price": 480.0}
    tco_breakdown = do_calculate_tco(weights, tolerances, supplier, bom_line)

    return {
        "ok": True,
        "system_design_proposal": sd_proposal,
        "product_enrichment": prod_enrichment,
        "procurement_tco": tco_breakdown,
    }


async def do_record_deal_loss_feedback(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Write a failure_pattern feedback edge in the Knowledge Graph.

    Ties a repeat loss reason to a product SKU as a feedback signal to Product.

    Params:
      - namespace_id: str | UUID (required)
      - sku: str (required, e.g., "SKU-XYZ")
      - loss_reason: str (required, e.g., "expensive" or "faulty")
      - confidence: float (optional, default 0.8)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    sku = params.get("sku")
    if not sku or not sku.strip():
        raise ValueError("sku is required")

    loss_reason = params.get("loss_reason")
    if not loss_reason or not loss_reason.strip():
        raise ValueError("loss_reason is required")

    confidence = float(params.get("confidence", 0.8))
    if not (0.0 <= confidence <= 1.0):
        raise ValueError("confidence must be between 0.0 and 1.0")

    subject_label = f"PRODUCT:{sku.upper().strip()}"
    predicate = "failure_pattern"
    object_label = f"LOSS_REASON:{loss_reason.upper().strip()}"

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, $2, $3, $4::float, $5::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    updated_at = NOW()
            """,
            subject_label,
            predicate,
            object_label,
            confidence,
            ns_uuid,
        )

    return {
        "ok": True,
        "edge": {
            "subject_label": subject_label,
            "predicate": predicate,
            "object_label": object_label,
            "confidence": confidence,
        },
    }
