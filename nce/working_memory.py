"""Working memory compilation and cognitive synthesis (Phase 3.4).

Synthesizes high-relevance episodic/consolidated memories, recursive KG neighborhood,
active procedural heuristics, and unresolved contradiction alerts within a strict
token budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.procedural_memory import fetch_relevant_procedural_heuristics
from nce.semantic_search import semantic_search

log = logging.getLogger("nce.working_memory")


def estimate_tokens(obj_or_str: Any) -> int:
    """Conservative deterministic token estimator.

    Uses max(word_count, (char_count + 3) // 4) to guarantee that both
    word-based and character-based token limits are strictly satisfied.
    """
    if isinstance(obj_or_str, str):
        text = obj_or_str
    else:
        text = json.dumps(obj_or_str, default=str)
    word_count = len(text.split())
    char_count = (len(text) + 3) // 4
    return max(1, max(word_count, char_count))


async def _fetch_kg_neighborhood(
    pool: Any,
    ns_uuid: UUID,
    goal: str,
    as_of: datetime | None,
) -> list[dict[str, Any]]:
    """Traverse KG neighborhood with recursive CTE and cycle guard."""
    # Extract candidate seed tokens from goal
    goal_words = [w for w in goal.split() if len(w) >= 3][:5]
    search_token = goal_words[0] if goal_words else goal[:30]

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            WITH RECURSIVE kg_walk AS (
                SELECT 
                    e.id,
                    e.subject_label,
                    e.predicate,
                    e.object_label,
                    e.confidence,
                    1 AS depth,
                    ARRAY[e.subject_label, e.object_label] AS visited_path
                FROM kg_edges e
                WHERE e.namespace_id = $1
                  AND ($2::timestamptz IS NULL OR e.created_at <= $2::timestamptz)
                  AND (
                      e.subject_label ILIKE '%' || $3 || '%'
                      OR e.object_label ILIKE '%' || $3 || '%'
                  )
                UNION ALL
                SELECT 
                    next_e.id,
                    next_e.subject_label,
                    next_e.predicate,
                    next_e.object_label,
                    next_e.confidence,
                    w.depth + 1 AS depth,
                    CASE
                        WHEN w.object_label = next_e.subject_label THEN w.visited_path || next_e.object_label
                        ELSE w.visited_path || next_e.subject_label
                    END AS visited_path
                FROM kg_edges next_e
                JOIN kg_walk w ON (
                    (w.object_label = next_e.subject_label AND NOT (next_e.object_label = ANY(w.visited_path)))
                    OR
                    (w.subject_label = next_e.object_label AND NOT (next_e.subject_label = ANY(w.visited_path)))
                )
                WHERE next_e.namespace_id = $1
                  AND ($2::timestamptz IS NULL OR next_e.created_at <= $2::timestamptz)
                  AND w.depth < 3
            )
            SELECT DISTINCT subject_label, predicate, object_label, confidence, depth
            FROM kg_walk
            ORDER BY depth ASC, confidence DESC
            LIMIT 50
            """,
            ns_uuid,
            as_of,
            search_token,
        )
        return [
            {
                "subject": r["subject_label"],
                "predicate": r["predicate"],
                "object": r["object_label"],
                "confidence": float(r["confidence"]),
                "depth": int(r["depth"]),
            }
            for r in rows
        ]


async def _fetch_contradictions(
    pool: Any,
    ns_uuid: UUID,
    as_of: datetime | None,
) -> list[dict[str, Any]]:
    """Query active unresolved contradictions within the temporal horizon."""
    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT id, memory_a_id, memory_b_id, agent_id, detected_at,
                   detection_path, signals, confidence, note
            FROM contradictions
            WHERE namespace_id = $1
              AND ($2::timestamptz IS NULL OR detected_at <= $2::timestamptz)
              AND resolved_at IS NULL
            ORDER BY confidence DESC, detected_at DESC
            LIMIT 10
            """,
            ns_uuid,
            as_of,
        )
        out = []
        for r in rows:
            d = dict(r)
            if "signals" in d and isinstance(d["signals"], str):
                try:
                    d["signals"] = json.loads(d["signals"])
                except Exception:
                    pass
            out.append(d)
        return out


async def _fetch_heuristics(
    pool: Any,
    ns_uuid: UUID,
    goal: str,
    as_of: datetime | None,
) -> list[dict[str, Any]]:
    """Query active procedural heuristics with its own scoped connection."""
    async with scoped_pg_session(pool, ns_uuid) as conn:
        return await fetch_relevant_procedural_heuristics(
            conn,
            ns_uuid,
            goal,
            limit=10,
            as_of=as_of,
        )


async def compile_working_memory(
    engine_or_deps: Any,
    *,
    goal: str,
    namespace_id: str | UUID | None = None,
    max_tokens: int = 4000,
    as_of: datetime | str | None = None,
    agent_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compile synthesized working memory context for an agent goal.

    Executes 4 parallel branches with isolated asyncpg connections:
      1. 3-Way Hybrid RRF semantic search
      2. Recursive CTE KG neighborhood with cycle protection
      3. Procedural heuristics retrieval
      4. Active unresolved contradiction alerts

    Deduplicates citations and strictly enforces the max_tokens budget.
    """
    if not goal or not str(goal).strip():
        raise ValueError("goal must be a non-empty string")

    # Resolve namespace UUID
    ns_raw = namespace_id or getattr(engine_or_deps, "default_namespace_id", None)
    if not ns_raw:
        raise ValueError("namespace_id is required for compile_working_memory")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    # Validate token budget
    token_budget = max(64, min(int(max_tokens or 4000), 32768))

    # Parse and validate as_of
    as_of_dt: datetime | None = None
    if as_of is not None:
        if isinstance(as_of, str):
            as_of_clean = as_of.strip()
            if as_of_clean:
                as_of_dt = datetime.fromisoformat(as_of_clean.replace("Z", "+00:00"))
        elif isinstance(as_of, datetime):
            as_of_dt = as_of
        if as_of_dt is not None and as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)

    # Extract database pool and dependencies
    pg_pool = getattr(engine_or_deps, "pg_pool", None)
    mongo_client = getattr(engine_or_deps, "mongo_client", None)
    embedding_fn = getattr(engine_or_deps, "embedding_fn", None)

    if pg_pool is None:
        # Check kwargs or dict style
        pg_pool = kwargs.get("pg_pool")
        mongo_client = kwargs.get("mongo_client", mongo_client)
        embedding_fn = kwargs.get("embedding_fn", embedding_fn)

    if pg_pool is None:
        raise ValueError("Database pool is required for compile_working_memory")

    # Branch 1: Hybrid RRF Semantic Search
    async def _run_search() -> list[dict[str, Any]]:
        try:
            if hasattr(engine_or_deps, "semantic_search"):
                return await engine_or_deps.semantic_search(
                    query=goal,
                    namespace_id=str(ns_uuid),
                    agent_id=agent_id or "default",
                    limit=10,
                    as_of=as_of_dt,
                )
            if embedding_fn and mongo_client:
                return await semantic_search(
                    pg_pool=pg_pool,
                    mongo_client=mongo_client,
                    embedding_fn=embedding_fn,
                    query=goal,
                    namespace_id=str(ns_uuid),
                    agent_id=agent_id or "default",
                    limit=10,
                    as_of=as_of_dt,
                )
        except Exception as exc:
            log.warning("compile_working_memory: semantic search branch failed: %s", exc)
        return []

    # Branch 2: Recursive CTE KG neighborhood
    async def _run_kg() -> list[dict[str, Any]]:
        try:
            return await _fetch_kg_neighborhood(pg_pool, ns_uuid, goal, as_of_dt)
        except Exception as exc:
            log.warning("compile_working_memory: KG branch failed: %s", exc)
            return []

    # Branch 3: Procedural heuristics
    async def _run_heuristics() -> list[dict[str, Any]]:
        try:
            return await _fetch_heuristics(pg_pool, ns_uuid, goal, as_of_dt)
        except Exception as exc:
            log.warning("compile_working_memory: procedural heuristics branch failed: %s", exc)
            return []

    # Branch 4: Contradictions
    async def _run_contradictions() -> list[dict[str, Any]]:
        try:
            return await _fetch_contradictions(pg_pool, ns_uuid, as_of_dt)
        except Exception as exc:
            log.warning("compile_working_memory: contradictions branch failed: %s", exc)
            return []

    # Execute all 4 branches concurrently on separate connections
    search_hits, kg_nodes_edges, procedural_rules, contradiction_alerts = await asyncio.gather(
        _run_search(),
        _run_kg(),
        _run_heuristics(),
        _run_contradictions(),
    )

    # Build structured provenance citations
    provenance_citations: list[dict[str, Any]] = []
    seen_mem_ids: set[str] = set()

    for mem in search_hits:
        m_id = str(mem.get("memory_id") or "")
        if m_id and m_id not in seen_mem_ids:
            seen_mem_ids.add(m_id)
            provenance_citations.append(
                {
                    "memory_id": m_id,
                    "payload_ref": str(mem.get("payload_ref") or ""),
                    "source_type": "semantic_search",
                    "confidence_score": float(
                        mem.get("confidence_score", mem.get("confidence", 0.5))
                    ),
                    "epistemic_warnings": list(mem.get("epistemic_warnings") or []),
                }
            )

    for rule in procedural_rules:
        r_id = str(rule.get("id") or "")
        if r_id and r_id not in seen_mem_ids:
            seen_mem_ids.add(r_id)
            provenance_citations.append(
                {
                    "memory_id": r_id,
                    "payload_ref": "",
                    "source_type": "procedural_heuristic",
                    "confidence_score": float(rule.get("confidence_weight", 0.8)),
                    "epistemic_warnings": [],
                }
            )

    result: dict[str, Any] = {
        "goal": goal,
        "as_of": as_of_dt.isoformat() if as_of_dt else None,
        "procedural_guardrails": procedural_rules,
        "contradiction_alerts": contradiction_alerts,
        "top_memories": search_hits,
        "knowledge_subgraph": kg_nodes_edges,
        "provenance_citations": provenance_citations,
        "token_count": 0,
    }

    # Strict token budget enforcement (greedy trimming)
    # Order of priority to keep: guardrails & contradiction alerts, then top memories, then KG
    result["token_count"] = estimate_tokens(result)
    while (
        result["token_count"] > token_budget
        or (len(json.dumps(result, default=str)) // 4) > token_budget
    ):
        if result["knowledge_subgraph"]:
            result["knowledge_subgraph"].pop()
        elif len(result["top_memories"]) > 1:
            popped = result["top_memories"].pop()
            p_id = str(popped.get("memory_id") or "")
            result["provenance_citations"] = [
                c
                for c in result["provenance_citations"]
                if not (c["source_type"] == "semantic_search" and c["memory_id"] == p_id)
            ]
        elif (
            result["top_memories"]
            and isinstance(result["top_memories"][0].get("raw_data"), str)
            and len(result["top_memories"][0]["raw_data"]) > 64
        ):
            current_raw = result["top_memories"][0]["raw_data"]
            result["top_memories"][0]["raw_data"] = current_raw[: max(32, len(current_raw) // 2)]
        elif len(result["procedural_guardrails"]) > 1:
            popped_rule = result["procedural_guardrails"].pop()
            r_id = str(popped_rule.get("id") or "")
            result["provenance_citations"] = [
                c
                for c in result["provenance_citations"]
                if not (c["source_type"] == "procedural_heuristic" and c["memory_id"] == r_id)
            ]
        elif len(result["contradiction_alerts"]) > 1:
            result["contradiction_alerts"].pop()
        elif result["top_memories"]:
            result["top_memories"].pop()
            result["provenance_citations"] = [
                c for c in result["provenance_citations"] if c["source_type"] != "semantic_search"
            ]
        elif result["contradiction_alerts"]:
            result["contradiction_alerts"].pop()
        elif result["procedural_guardrails"]:
            result["procedural_guardrails"].pop()
            result["provenance_citations"] = []
        else:
            break
        result["token_count"] = estimate_tokens(result)

    return result
