"""
nce/vertical_modules/sales/ai.py
=================================
AI Surface logic for Sales (lead scoring, quote-draft assist, win/loss recall).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.embeddings import embed
from nce.event_log import append_event
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.sales.ai")


async def recall_similar_deals(
    conn: Any,
    namespace_id: UUID,
    query_text: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Recall similar won/lost deals/quotes/leads from the memories table.

    Uses cosine similarity over memories.
    """
    query_vec = await embed(query_text)

    rows = await conn.fetch(
        """
        SELECT id,
               name,
               payload_ref,
               node_type,
               metadata,
               embedding <=> $1::vector AS distance,
               1 - (embedding <=> $1::vector) AS similarity
        FROM memories
        WHERE namespace_id = $2::uuid
          AND node_type IN ('DEAL', 'QUOTE', 'LEAD', 'OPPORTUNITY')
          AND embedding IS NOT NULL
          AND valid_to IS NULL
        ORDER BY distance ASC
        LIMIT $3
        """,
        json.dumps(query_vec),
        namespace_id,
        top_k,
    )
    return [dict(r) for r in rows]


async def do_win_loss_recall(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve similar deals/quotes from memories.

    Params:
      - namespace_id: str | UUID (required)
      - query_text: str (required)
      - top_k: int (optional, default 5)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    query_text = params.get("query_text")
    if not query_text:
        raise ValueError("query_text is required")

    top_k = params.get("top_k", 5)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        candidates = await recall_similar_deals(conn, ns_uuid, query_text, top_k)

    return {
        "ok": True,
        "candidates": candidates,
    }


async def do_score_lead(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Calculate lead score and confidence from similar historical deals.

    Propose-only, never auto-accept.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    query_text = params.get("query_text")
    if not query_text:
        query_text = params.get("lead_name") or params.get("subject") or "lead"

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        candidates = await recall_similar_deals(conn, ns_uuid, query_text, top_k=5)

        won_count = 0
        total_count = 0
        total_similarity = 0.0

        for cand in candidates:
            # Only consider actually similar memories
            if cand.get("similarity", 0.0) < 0.6:
                continue

            meta = {}
            raw_meta = cand.get("metadata")
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            elif isinstance(raw_meta, dict):
                meta = raw_meta

            outcome = meta.get("outcome") or meta.get("status")
            if outcome == "won":
                won_count += 1
            total_count += 1
            total_similarity += cand.get("similarity", 0.0)

        if total_count > 0:
            score = won_count / total_count
            confidence = total_similarity / total_count
            reasons = [
                f"Found {total_count} similar historical deals.",
                f"Historical win rate among these deals is {score * 100:.1f}%.",
            ]
        else:
            score = 0.5  # Neutral default
            confidence = 0.5
            reasons = ["No similar historical deals found in memory. Defaulted to neutral score."]

    return {
        "ok": True,
        "score": score,
        "confidence": confidence,
        "propose_only": True,
        "reasons": reasons,
    }


async def do_draft_quote(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """AI Quote-Draft Assist (Advisor).

    Propose-only, never auto-accept.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    opportunity_id = params.get("opportunity_id")
    description = params.get("description") or opportunity_id or "quote draft"

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        candidates = await recall_similar_deals(conn, ns_uuid, description, top_k=3)

        proposed_lines: list[dict[str, Any]] = []
        suggested_margin_pct = 0.35
        total_margin = 0.0
        margin_count = 0

        for cand in candidates:
            # Only consider actually similar memories
            if cand.get("similarity", 0.0) < 0.6:
                continue

            meta = {}
            raw_meta = cand.get("metadata")
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            elif isinstance(raw_meta, dict):
                meta = raw_meta

            lines = meta.get("lines") or []
            for line in lines:
                if line.get("product_ref") not in [x.get("product_ref") for x in proposed_lines]:
                    proposed_lines.append(
                        {
                            "product_ref": line.get("product_ref"),
                            "qty": line.get("qty", 1),
                            # No price in the recalled memory means "unknown", not 100.0.
                            # None carries the absence so a consumer can tell it from a real
                            # price (no-fabricated-money-defaults).
                            "suggested_unit_price": line.get("unit_price"),
                        }
                    )
            margin_pct = meta.get("margin_pct") or meta.get("signed_margin_pct")
            if margin_pct is not None:
                total_margin += float(margin_pct)
                margin_count += 1

        if margin_count > 0:
            suggested_margin_pct = total_margin / margin_count

    return {
        "ok": True,
        "proposed_lines": proposed_lines,
        "suggested_margin_pct": suggested_margin_pct,
        "propose_only": True,
        "validated": False,
    }


async def do_record_ai_decision(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record an AI accept/override decision to the ledger (event_log)."""
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    agent_id = params.get("agent_id") or "sales-ai-agent"
    decision_type = params.get("decision_type")
    if not decision_type:
        raise ValueError("decision_type is required")

    decision_details = params.get("decision_details") or {}

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        async with conn.transaction():
            event = await append_event(
                conn=conn,
                namespace_id=ns_uuid,
                agent_id=agent_id,
                event_type="sales_ai_decision",
                params={
                    "decision_type": decision_type,
                    "details": decision_details,
                },
            )
            event_id = str(event.event_id)
            event_seq = event.event_seq

    return {
        "ok": True,
        "event_id": event_id,
        "event_seq": event_seq,
        "status": "logged",
    }
