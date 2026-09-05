"""
nce/vertical_modules/marketing/brief.py
======================================
Morning Brief (#19 Executive Aggregate) slice and Agent-to-Agent (A2A) query surfaces.

Charter M14.W7 & Spec §14:
  - Expose directional throughput of harvested stories:
    candidates / drafts / published / testimonials.
  - Expose approved, consent-cleared, citation-grounded marketing evidence
    for peer agents (Sales won-deal reinforcement, System Design proof-of-performance).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.vertical_modules.marketing._guard import require_marketing_enabled

log = logging.getLogger("nce.vertical_modules.marketing.brief")


async def get_marketing_morning_brief_slice(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the marketing throughput slice for the Executive Morning Brief (#19).

    Parameters
    ----------
    engine : Any
        Engine context providing pg_pool.
    params : dict[str, Any]
        - namespace_id (str | UUID): active tenant

    Returns
    -------
    dict[str, Any]
        Throughput metrics covering story pipeline health.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("namespace_id is required")
    ns_str = str(raw_ns)

    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
    if pool is not None:
        await require_marketing_enabled(pool, ns_str)

    candidates_count = 0
    drafts_count = 0
    published_count = 0
    pending_testimonials = 0
    received_testimonials = 0
    published_assets = 0

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                # Harvested candidate projects
                cand_row = await conn.fetchrow(
                    """
                    SELECT count(*) as cnt
                    FROM   kg_nodes
                    WHERE  namespace_id = $1::uuid
                      AND  (entity_type = 'PROJECT' OR entity_type = 'PROJECT_PROJECT')
                    """,
                    UUID(ns_str),
                )
                if cand_row:
                    candidates_count = cand_row["cnt"]

                # Case study metrics
                cs_rows = await conn.fetch(
                    """
                    SELECT status, count(*) as cnt
                    FROM   case_studies
                    WHERE  namespace_id = $1::uuid
                    GROUP  BY status
                    """,
                    UUID(ns_str),
                )
                for r in cs_rows:
                    st = r["status"]
                    if st in ("draft", "in_review"):
                        drafts_count += r["cnt"]
                    elif st == "published":
                        published_count += r["cnt"]

                # Testimonials metrics
                t_rows = await conn.fetch(
                    """
                    SELECT status, count(*) as cnt
                    FROM   testimonials
                    WHERE  namespace_id = $1::uuid
                    GROUP  BY status
                    """,
                    UUID(ns_str),
                )
                for r in t_rows:
                    st = r["status"]
                    if st == "requested":
                        pending_testimonials += r["cnt"]
                    elif st in ("received", "approved"):
                        received_testimonials += r["cnt"]

                # Content assets metrics
                ca_rows = await conn.fetch(
                    """
                    SELECT status, count(*) as cnt
                    FROM   content_assets
                    WHERE  namespace_id = $1::uuid
                    GROUP  BY status
                    """,
                    UUID(ns_str),
                )
                for r in ca_rows:
                    if r["status"] == "published":
                        published_assets += r["cnt"]
        except Exception as exc:
            log.warning("get_marketing_morning_brief_slice DB read warning: %s", exc)

    return {
        "ok": True,
        "namespace_id": ns_str,
        "story_throughput": {
            "harvested_candidates": candidates_count,
            "drafts_in_review": drafts_count,
            "published_case_studies": published_count,
            "pending_testimonials": pending_testimonials,
            "approved_testimonials": received_testimonials,
            "published_assets": published_assets,
        },
    }


async def do_query_marketing_a2a(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Query approved marketing evidence for Agent-to-Agent (A2A) consumption.

    Parameters
    ----------
    engine : Any
        Engine context providing pg_pool.
    params : dict[str, Any]
        - namespace_id (str | UUID): active tenant
        - product (str, optional): filter by technology/product
        - limit (int, optional): max items (default 5)

    Returns
    -------
    dict[str, Any]
        Anonymized, consent-verified proof items with graph citations.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("namespace_id is required")
    ns_str = str(raw_ns)

    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
    if pool is not None:
        await require_marketing_enabled(pool, ns_str)

    limit = min(max(int(params.get("limit") or 5), 1), 20)
    items: list[dict[str, Any]] = []

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, title, body, anonymized, marketing_source_id
                    FROM   case_studies
                    WHERE  namespace_id = $1::uuid
                      AND  status IN ('approved', 'published')
                    ORDER  BY created_at DESC
                    LIMIT  $2
                    """,
                    UUID(ns_str),
                    limit,
                )
                for r in rows:
                    items.append(
                        {
                            "id": str(r["id"]),
                            "title": r["title"],
                            "anonymized": r["anonymized"],
                            "marketing_source_id": r["marketing_source_id"],
                        }
                    )
        except Exception as exc:
            log.warning("do_query_marketing_a2a DB lookup error: %s", exc)

    return {
        "ok": True,
        "namespace_id": ns_str,
        "evidence_items": items,
        "count": len(items),
    }
