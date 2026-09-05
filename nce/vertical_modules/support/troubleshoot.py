"""
nce/vertical_modules/support/troubleshoot.py
============================================
AI Troubleshooter cognitive recall for Module 10 (Support Engine):
  - do_troubleshoot: recalls similar past tickets and structured resolutions
    from v3_cognitive_ledger (and memories when available)
  - Cites historical ticket IDs and resolutions (auditable)
  - Honest zero-history fallback (Contract-H)

Strict Tenant Predicate Discipline (Charter §5.5)
-------------------------------------------------
Every query against v3_cognitive_ledger and service_tickets enforces
explicit WHERE namespace_id = $1::uuid predicates.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.support.tickets import (
    TicketNotFoundError,
    _extract_pool,
    _parse_uuid,
)

log = logging.getLogger("nce.vertical_modules.support.troubleshoot")

EVENT_TYPE_DIAGNOSIS_AUTHORED: str = "support_diagnosis_authored"

_WORD_RE = re.compile(r"[a-zA-Z0-9_\-]+")


def _tokenize(text: str) -> set[str]:
    """Tokenize and normalize text into non-trivial word tokens."""
    if not text:
        return set()
    return {
        w.lower()
        for w in _WORD_RE.findall(text)
        if len(w) > 2
        and w.lower()
        not in {
            "the",
            "and",
            "for",
            "with",
            "this",
            "that",
            "from",
            "has",
            "was",
            "are",
        }
    }


def _calculate_similarity(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    """Calculate token overlap similarity ratio."""
    if not query_tokens or not candidate_tokens:
        return 0.0
    intersection = query_tokens.intersection(candidate_tokens)
    if not intersection:
        return 0.0
    return len(intersection) / len(query_tokens.union(candidate_tokens))


async def do_troubleshoot(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Recall similar past incidents and proposed fixes from cognitive memory.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine instance or asyncpg.Pool.
    params:
        - namespace_id: (required) tenant UUID string or UUID.
        - ticket_id: (optional) active ticket UUID to diagnose.
        - asset_id: (optional) target asset UUID.
        - symptom_text: (optional) observed symptoms / error text.
        - limit: (optional) maximum citations to return (default 5).
        - min_confidence: (optional) minimum confidence score to propose fix (default 0.5).
    """
    pool = _extract_pool(engine_or_pool)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    ticket_id_raw = params.get("ticket_id")
    symptom_text = str(params.get("symptom_text") or "").strip()
    target_asset_id = params.get("asset_id")
    limit = max(int(params.get("limit", 5)), 1)
    min_confidence = float(params.get("min_confidence", 0.5))

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. If ticket_id is supplied, load ticket context if symptom_text not given
        if ticket_id_raw is not None:
            ticket_id = _parse_uuid(ticket_id_raw, "ticket_id")
            ticket_row = await conn.fetchrow(
                """
                SELECT id, namespace_id, summary, description, asset_id
                FROM service_tickets
                WHERE id = $1::uuid AND namespace_id = $2::uuid
                """,
                ticket_id,
                ns_uuid,
            )
            if ticket_row is None:
                raise TicketNotFoundError(ticket_id=str(ticket_id))

            if not symptom_text:
                desc = ticket_row["description"] or ""
                symptom_text = f"{ticket_row['summary']} {desc}".strip()
            if target_asset_id is None and ticket_row["asset_id"]:
                target_asset_id = str(ticket_row["asset_id"])

        if not symptom_text:
            raise ValueError(
                "symptom_text or a valid ticket_id with summary/description is required"
            )

        query_tokens = _tokenize(symptom_text)
        target_asset_str = str(target_asset_id).lower() if target_asset_id else None

        # 2. Query structured resolutions from v3_cognitive_ledger
        # Strict namespace_id predicate enforcement per Charter §5.5
        rows = await conn.fetch(
            """
            SELECT id, namespace_id, tlx_scores, created_at
            FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND tlx_scores->>'event_type' = 'ticket_resolution'
            ORDER BY created_at DESC
            LIMIT 100
            """,
            ns_uuid,
        )

        candidates: list[dict[str, Any]] = []
        for r in rows:
            raw_tlx = r["tlx_scores"]
            if not raw_tlx:
                continue
            payload = json.loads(raw_tlx) if isinstance(raw_tlx, str) else dict(raw_tlx)

            res_text = payload.get("resolution_text")
            summary = payload.get("summary") or ""
            if not res_text or not payload.get("was_fix", True):
                continue

            cand_tokens = _tokenize(f"{summary} {res_text}")
            token_sim = _calculate_similarity(query_tokens, cand_tokens)

            # Match bonus checks
            asset_match = False
            fixed_asset = str(payload.get("fixed_asset_id") or "").lower()
            if target_asset_str and fixed_asset and target_asset_str == fixed_asset:
                asset_match = True

            # Calculate confidence score
            if asset_match and token_sim > 0.1:
                confidence = min(0.60 + (token_sim * 0.40), 0.98)
            elif token_sim >= 0.20:
                confidence = min(0.50 + (token_sim * 0.45), 0.92)
            elif token_sim > 0.05:
                confidence = min(0.35 + (token_sim * 0.50), 0.85)
            elif asset_match:
                confidence = 0.65
            else:
                confidence = 0.0

            if confidence >= min_confidence:
                candidates.append(
                    {
                        "ledger_id": str(r["id"]),
                        "ticket_id": payload.get("ticket_id"),
                        "summary": summary,
                        "resolution_text": res_text,
                        "resolution_category": payload.get("resolution_category", "other"),
                        "fixed_asset_id": payload.get("fixed_asset_id"),
                        "confidence": round(confidence, 2),
                        "created_at": r["created_at"].isoformat()
                        if hasattr(r["created_at"], "isoformat")
                        else str(r["created_at"]),
                    }
                )

        # 3. Sort candidates by confidence
        candidates.sort(key=lambda c: float(c["confidence"]), reverse=True)
        top_matches = candidates[:limit]

        if not top_matches:
            # Honest zero-history fallback per Charter §2.1 & Review Round 2
            return {
                "diagnosis": "No matching past resolution patterns found for this asset or symptom.",
                "proposed_fix": None,
                "confidence": 0.0,
                "cited_ticket_ids": [],
                "similar_tickets": [],
                "sources_count": 0,
            }

        top = top_matches[0]
        cited_ids = [c["ticket_id"] for c in top_matches if c.get("ticket_id")]

        diagnosis = (
            f"Pattern match identified ({top['resolution_category']}): "
            f"Prior resolution from ticket {top['ticket_id'] or 'history'} applies."
        )

        return {
            "diagnosis": diagnosis,
            "proposed_fix": top["resolution_text"],
            "confidence": top["confidence"],
            "cited_ticket_ids": cited_ids,
            "similar_tickets": top_matches,
            "sources_count": len(top_matches),
            "event_type": EVENT_TYPE_DIAGNOSIS_AUTHORED,
        }
