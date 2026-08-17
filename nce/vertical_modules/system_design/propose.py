"""
nce/vertical_modules/system_design/propose.py
=============================================
AI-Solution-Agent recall loop for the System Design vertical module
(Wave 3 — Batch 058).

Entry-point: ``do_propose_design(engine, params) -> dict``

Design invariants (from the wave brief):
  - PROPOSE-ONLY: the result is a proposed BOM; nothing is auto-accepted,
    frozen, or applied.  ``validated: False`` is a field of the RETURNED
    Python dict only — it is NOT a column and NOT persisted.
  - SIMILARITY-FIRST: ranking is pure cosine similarity (1 - distance)
    over the ``memories`` table (halfvec pgvector).
  - DORMANT outcome-weighting: ``_apply_outcome_weights`` is a stub gated
    by ``NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED`` (default False).
    When OFF, candidates are returned unchanged in pure-similarity order.
  - No migration, no new node types, no surface change (no MCP tool).
  - ``confidence`` lives on edges only (rule 7); this module does not
    write any kg_nodes or kg_edges — it is RETURN-ONLY (see report).

Return-only rationale:
  ``do_author_functional_location`` requires a fully specified
  site/building/floor/room hierarchy to write DESIGN_LINE nodes.  A
  recall-based propose does not have that structural context — it
  surfaces BOM lines for human review before any design is authored.
  Persisting speculative nodes into kg_nodes would couple the propose
  step to a write path that has no ``validated`` column to mark them
  as unconfirmed.  The correct lifecycle is: human confirms → Wave 4
  calls ``do_author_functional_location`` with the confirmed lines.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.embeddings import embed

log = logging.getLogger("nce.vertical_modules.system_design.propose")

# Node types in ``memories`` that represent past design / project evidence.
_RECALL_NODE_TYPES: list[str] = ["DESIGN", "PROJECT"]


# ---------------------------------------------------------------------------
# Private: pgvector recall over memories
# ---------------------------------------------------------------------------


async def _recall_similar_designs(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    query_vec: list[float],
    top_k: int,
) -> list[dict[str, Any]]:
    """Recall the top-K most similar DESIGN/PROJECT memories for *ns_uuid*.

    Uses pgvector cosine distance (``<=>``).  Returns rows ordered by
    ascending distance (most similar first) with a derived ``similarity``
    score (= 1 - distance).

    Only memories with a non-NULL embedding and NULL ``valid_to``
    (i.e. not soft-deleted) are considered.

    Parameters
    ----------
    conn:
        asyncpg connection with the RLS namespace GUC already set by
        ``scoped_pg_session``.
    ns_uuid:
        Tenant namespace UUID.
    query_vec:
        768-dim L2-normalised query vector from ``nce.embeddings.embed``.
    top_k:
        Maximum number of candidates to return (>= 1).

    Returns
    -------
    list[dict]
        Each dict has keys: ``id``, ``name``, ``payload_ref``,
        ``node_type``, ``metadata``, ``distance``, ``similarity``.
    """
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
          AND node_type    = ANY($3::text[])
          AND embedding    IS NOT NULL
          AND valid_to     IS NULL
        ORDER BY distance ASC
        LIMIT $4
        """,
        json.dumps(query_vec),
        str(ns_uuid),
        _RECALL_NODE_TYPES,
        top_k,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Private: outcome-weighting hook (DORMANT)
# ---------------------------------------------------------------------------


def _apply_outcome_weights(
    candidates: list[dict[str, Any]],
    *,
    enabled: bool,
) -> list[dict[str, Any]]:
    """Apply outcome-based discount factors to recall candidates.

    DORMANT — when ``enabled`` is False (the only supported mode for now),
    candidates are returned UNCHANGED in pure cosine-similarity order.

    Future activation (once Project/Support engines backfill the ledger):
      - Discount scores by change-order frequency (more COs → lower weight).
      - Discount by open support-ticket pressure.
      - Boost by gross-margin realisation rate.
    The maths will live here when ``NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED``
    is flipped to True.  Until then, do NOT reorder — ranking is pure similarity.
    """
    if not enabled:
        # Pure-similarity order: return unchanged.
        return candidates

    # TODO (Wave N): implement discount-by-change-orders/tickets/margin once
    # Project and Support engines publish their ledger entries to
    # v3_cognitive_ledger.  Gate on NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED.
    raise NotImplementedError(
        "_apply_outcome_weights is dormant; set "
        "NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED=false (default) "
        "to use pure-similarity ranking."
    )


# ---------------------------------------------------------------------------
# Private: build proposed BOM lines from recall evidence
# ---------------------------------------------------------------------------


def _build_proposed_lines(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert recall evidence rows into proposed DESIGN_LINE dicts.

    Reads ``product_ref`` and ``qty`` from the recalled memory's ``metadata``
    JSONB.  Rows that have no usable ``product_ref`` in their metadata are
    skipped gracefully.

    Each returned line has shape::

        {
            "product_ref": str,       # pulled from memory.metadata
            "qty": int | float,       # pulled from memory.metadata, default 1
            "confidence": float,      # cosine similarity (0–1), edge-only semantics
            "validated": False,       # PROPOSE-ONLY invariant — never True here
            "recall_memory_id": str,  # traceability back to the source memory
        }

    ``validated: False`` is a Python dict field only — no kg_nodes column.
    """
    lines: list[dict[str, Any]] = []
    for row in candidates:
        meta: dict[str, Any] = {}
        raw_meta = row.get("metadata")
        if isinstance(raw_meta, str):
            try:
                meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = raw_meta

        product_ref = meta.get("product_ref") or meta.get("product_ref_id") or ""
        if not product_ref:
            log.debug(
                "propose: skipping memory %s — no product_ref in metadata",
                row.get("id"),
            )
            continue

        lines.append(
            {
                "product_ref": str(product_ref),
                "qty": meta.get("qty", meta.get("quantity", 1)),
                "confidence": float(row.get("similarity", 0.0)),
                "validated": False,  # PROPOSE-ONLY: humans confirm 100%
                "recall_memory_id": str(row.get("id", "")),
            }
        )
    return lines


# ---------------------------------------------------------------------------
# Public: do_propose_design
# ---------------------------------------------------------------------------


async def do_propose_design(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Embed a room brief, recall the most similar past designs, and return
    a proposed BOM of DESIGN_LINE dicts.

    This is a PROPOSE-ONLY function.  It never auto-accepts, freezes, or
    applies any line.  Every returned line has ``validated: False``.
    The caller (human or downstream tool) must explicitly confirm each line
    before it is authored into the knowledge graph.

    Parameters
    ----------
    engine:
        NCEEngine instance.  Must have a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "room_brief": str,           # natural-language description of the room
                                         # / design requirement to be matched
        }``

    Returns
    -------
    dict
        ``{
            "proposed_lines": [
                {
                    "product_ref": str,
                    "qty": int | float,
                    "confidence": float,   # cosine similarity (0–1)
                    "validated": False,    # always False — propose-only
                    "recall_memory_id": str,
                }
            ],
            "recall_evidence": [
                {
                    "id": str,
                    "name": str | None,
                    "payload_ref": str,
                    "node_type": str,
                    "similarity": float,
                    "distance": float,
                }
            ],
            "outcome_weighting_applied": bool,  # False when dormant flag is OFF
        }``
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_propose_design: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    room_brief: str = params.get("room_brief", "")
    if not room_brief:
        raise ValueError("do_propose_design: 'room_brief' is required in params")

    top_k: int = cfg.NCE_SYSTEM_DESIGN_RECALL_TOP_K
    outcome_weighting_enabled: bool = cfg.NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED

    # 1. Embed the room brief (outside the DB transaction per scoped_pg_session
    #    contract — no slow I/O inside the transaction).
    query_vec: list[float] = await embed(room_brief)

    # 2. Recall top-K similar DESIGN/PROJECT memories (RLS-scoped).
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        candidates = await _recall_similar_designs(conn, ns_uuid, query_vec, top_k)

    log.info(
        "do_propose_design: ns=%s recalled %d candidate(s) (top_k=%d)",
        ns_uuid,
        len(candidates),
        top_k,
    )

    # 3. Apply (dormant) outcome-weighting.
    ranked = _apply_outcome_weights(candidates, enabled=outcome_weighting_enabled)

    # 4. Build proposed BOM lines from ranked evidence.
    proposed_lines = _build_proposed_lines(ranked)

    # 5. Strip DB internals from evidence for the return payload.
    recall_evidence = [
        {
            "id": str(r.get("id", "")),
            "name": r.get("name"),
            "payload_ref": r.get("payload_ref", ""),
            "node_type": r.get("node_type", ""),
            "similarity": float(r.get("similarity", 0.0)),
            "distance": float(r.get("distance", 0.0)),
        }
        for r in ranked
    ]

    return {
        "proposed_lines": proposed_lines,
        "recall_evidence": recall_evidence,
        "outcome_weighting_applied": outcome_weighting_enabled,
    }
