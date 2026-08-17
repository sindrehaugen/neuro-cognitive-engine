"""
nce/vertical_modules/vendors/performance.py
===========================================
Contractor performance calculations and cognitive recall for Vendors Axis (Batch 103).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.embeddings import embed

log = logging.getLogger("nce.vertical_modules.vendors.performance")


def parse_json(val: Any) -> Any:
    """Parse JSON field values helper (handles both dict and string representations)."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return val or {}


async def do_compute_performance(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compute contractor performance from work-order ratings on the ledger.

    Calculates rolling window average of ratings, normalizes to 0-100 scale,
    and updates contractor_profiles table.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    contractor_id = params.get("contractor_id")
    if not contractor_id:
        raise ValueError("contractor_id is required")

    contractor_id_str = str(contractor_id).strip()
    if not contractor_id_str.startswith("CONTRACTOR:"):
        contractor_id_str = f"CONTRACTOR:{contractor_id_str.upper()}"

    window_days = params.get("window")
    if window_days is None:
        window_days = getattr(cfg, "NCE_VENDORS_SCORECARD_WINDOW_DAYS", 365)
    if window_days is None:
        window_days = 365
    window_days = int(window_days)

    ratings: list[float] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Fetch work order rating events from v3_cognitive_ledger
        rows = await conn.fetch(
            """
            SELECT tlx_scores, created_at
            FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND tlx_scores->>'contractor_id' = $2
              AND tlx_scores->>'event_type' = 'work_order_rating'
              AND tlx_scores->'rating' IS NOT NULL
              AND created_at >= NOW() - ($3::int * INTERVAL '1 day')
            ORDER BY created_at DESC
            """,
            ns_uuid,
            contractor_id_str,
            window_days,
        )

        for row in rows:
            scores = parse_json(row["tlx_scores"])
            rating_val = scores.get("rating")
            if rating_val is not None:
                ratings.append(float(rating_val))

    sample_n = len(ratings)
    min_sample = getattr(cfg, "NCE_VENDORS_SCORECARD_MIN_SAMPLE", 5)

    if sample_n < min_sample:
        performance_score = None
        insufficient_data = True
    else:
        avg_rating = sum(ratings) / sample_n
        performance_score = round(avg_rating * 20.0, 2)
        insufficient_data = False

    # Update postgres contractor profile performance score
    if hasattr(engine, "pg_pool") and engine.pg_pool:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            await conn.execute(
                """
                UPDATE contractor_profiles
                SET performance_score = $1, updated_at = NOW()
                WHERE contractor_id = $2 AND namespace_id = $3
                """,
                performance_score,
                contractor_id_str,
                ns_uuid,
            )

    return {
        "ok": True,
        "contractor_id": contractor_id_str,
        "performance_score": performance_score,
        "sample_n": sample_n,
        "insufficient_data": insufficient_data,
    }


async def do_recall_similar_jobs(
    engine: Any,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Perform cognitive similarity search over jobs/reviews and link outcome ratings."""
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    query = params.get("query")
    if not query:
        raise ValueError("query is required")

    contractor_id = params.get("contractor_id")
    top_k_val = params.get("top_k") or params.get("limit", 5)
    top_k = max(1, int(top_k_val))

    # Embed the search query
    vector = await embed(query)
    vector_json = json.dumps(vector)

    sql = """
    SELECT m.id,
           m.name,
           m.metadata,
           cl.tlx_scores,
           1 - (m.embedding <=> $1::vector) AS similarity
    FROM memories m
    LEFT JOIN v3_cognitive_ledger cl ON m.id = cl.memory_id
    WHERE m.namespace_id = $2::uuid
      AND m.node_type IN ('CONTRACTOR_JOB', 'WORK_ORDER', 'CONTRACTOR_REVIEW')
      AND m.embedding IS NOT NULL
      AND m.valid_to IS NULL
    """
    args: list[Any] = [vector_json, ns_uuid]

    if contractor_id:
        contractor_id_str = str(contractor_id).strip()
        if not contractor_id_str.startswith("CONTRACTOR:"):
            contractor_id_str = f"CONTRACTOR:{contractor_id_str.upper()}"
        sql += """
        AND (
            m.metadata->>'contractor_id' = $3
            OR cl.tlx_scores->>'contractor_id' = $3
            OR m.name = $3
        )
        """
        args.append(contractor_id_str)
        limit_placeholder = "$4"
    else:
        limit_placeholder = "$3"

    sql += f"""
    ORDER BY m.embedding <=> $1::vector ASC
    LIMIT {limit_placeholder}
    """
    args.append(top_k)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(sql, *args)

    results: list[dict[str, Any]] = []
    for row in rows:
        meta = parse_json(row["metadata"])
        tlx = parse_json(row["tlx_scores"])

        desc = (
            meta.get("description")
            or meta.get("content")
            or tlx.get("description")
            or row["name"]
            or ""
        )
        c_id = (
            tlx.get("contractor_id")
            or meta.get("contractor_id")
            or (row["name"] if row["name"] and row["name"].startswith("CONTRACTOR:") else None)
        )
        wo_id = tlx.get("work_order_id") or meta.get("work_order_id")

        rating_val = tlx.get("rating")
        if rating_val is None:
            rating_val = meta.get("rating")
        rating = float(rating_val) if rating_val is not None else None

        results.append(
            {
                "memory_id": str(row["id"]),
                "contractor_id": c_id,
                "work_order_id": wo_id,
                "description": desc,
                "rating": rating,
                "similarity": float(row["similarity"]) if row["similarity"] is not None else 0.0,
            }
        )

    return results
