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
  - OUTCOME-WEIGHTING: ``_apply_outcome_weights`` adjusts recall scores
    based on attributed project outcomes from ``kg_edges`` (predicate ``has_outcome``).
    Gated by ``NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED`` (default True).
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
    outcomes: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply outcome-based discount/boost factors to recall candidates (Wave SD-2).

    When ``enabled`` is True and outcome data exists:
      - Projects with successful outcomes (high confidence / positive margin) are boosted.
      - Projects with slips / rework are discounted.
    Computes a strict coverage indicator:
      - "similarity-only: 0 attributed outcomes" when no outcomes exist.
      - "outcome-weighted: M/N attributed outcomes" when M outcomes are attributed.
    """
    outcomes = outcomes or {}
    total_count = len(candidates)
    attributed_count = 0

    enriched: list[dict[str, Any]] = []
    for c in candidates:
        cand = dict(c)
        name = cand.get("name") or ""
        proj_meta = cand.get("metadata")
        if isinstance(proj_meta, str):
            try:
                proj_meta = json.loads(proj_meta)
            except (json.JSONDecodeError, TypeError):
                proj_meta = {}
        elif not isinstance(proj_meta, dict):
            proj_meta = {}

        proj_id = str(proj_meta.get("project_id") or name)
        outcome_data = (
            outcomes.get(proj_id)
            or outcomes.get(name)
            or outcomes.get(f"PROJECT:{proj_id}")
            or outcomes.get(proj_id.removeprefix("PROJECT:"))
            or (outcomes.get(f"PROJECT:{name}") if name else None)
            or (outcomes.get(name.removeprefix("PROJECT:")) if name else None)
        )

        if outcome_data:
            attributed_count += 1
            cand["attributed_outcome"] = True
            conf = float(outcome_data.get("confidence", 1.0))
            cand["outcome_confidence"] = conf
            drift = float(outcome_data.get("margin_drift", 0.0))
            weight_factor = (
                max(0.2, min(2.0, 1.0 + drift))
                if drift != 0.0
                else max(0.2, min(2.0, 1.0 + (conf - 0.5) * 0.5))
            )
            base_sim = float(cand.get("similarity", 0.0))
            cand["weighted_score"] = round(base_sim * weight_factor, 4)
        else:
            cand["attributed_outcome"] = False
            cand["outcome_confidence"] = None
            cand["weighted_score"] = round(float(cand.get("similarity", 0.0)), 4)

        enriched.append(cand)

    if enabled and attributed_count > 0:
        enriched.sort(key=lambda x: x.get("weighted_score", 0.0), reverse=True)

    status = (
        "similarity-only: 0 attributed outcomes"
        if attributed_count == 0
        else f"outcome-weighted: {attributed_count}/{total_count} attributed outcomes"
    )
    coverage = {
        "attributed_outcomes": attributed_count,
        "total_candidates": total_count,
        "status": status,
    }
    return enriched, coverage


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

    raw_top_k = params.get("top_k")
    if raw_top_k is not None:
        try:
            val = int(raw_top_k)
            top_k = max(1, min(val, 50))
        except (TypeError, ValueError):
            top_k = cfg.NCE_SYSTEM_DESIGN_RECALL_TOP_K
    else:
        top_k = cfg.NCE_SYSTEM_DESIGN_RECALL_TOP_K
    outcome_weighting_enabled: bool = cfg.NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED

    # 1. Embed the room brief (outside the DB transaction per scoped_pg_session
    #    contract — no slow I/O inside the transaction).
    query_vec: list[float] = await embed(room_brief)

    # 2. Recall top-K similar DESIGN/PROJECT memories (RLS-scoped) and fetch outcome evidence.
    outcomes: dict[str, dict[str, Any]] = {}
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        candidates = await _recall_similar_designs(conn, ns_uuid, query_vec, top_k)

        # Check per-namespace outcome weighting override
        ns_metadata_row = await conn.fetchrow(
            "SELECT metadata FROM namespaces WHERE id = $1::uuid",
            str(ns_uuid),
        )
        ns_metadata = (
            json.loads(ns_metadata_row["metadata"])
            if ns_metadata_row and isinstance(ns_metadata_row["metadata"], str)
            else (ns_metadata_row["metadata"] if ns_metadata_row else {})
        ) or {}
        ns_override = ns_metadata.get("system_design", {}).get("outcome_weighting_enabled")
        if ns_override is not None:
            outcome_weighting_enabled = bool(ns_override)

        cand_keys: set[str] = set()
        for r in candidates:
            cname = r.get("name")
            if cname:
                cand_keys.add(str(cname))
                cand_keys.add(f"PROJECT:{cname}")
                cand_keys.add(str(cname).removeprefix("PROJECT:"))
            cmeta = r.get("metadata")
            if isinstance(cmeta, str):
                try:
                    cmeta = json.loads(cmeta)
                except Exception:
                    cmeta = {}
            if isinstance(cmeta, dict):
                pid = cmeta.get("project_id")
                if pid:
                    cand_keys.add(str(pid))
                    cand_keys.add(f"PROJECT:{pid}")

        if outcome_weighting_enabled and cand_keys:
            outcome_rows = await conn.fetch(
                """
                SELECT subject_label, confidence, object_label
                FROM kg_edges
                WHERE namespace_id = $1::uuid
                  AND predicate = 'has_outcome'
                  AND subject_label = ANY($2::text[])
                """,
                str(ns_uuid),
                list(cand_keys),
            )
            for row in outcome_rows:
                subj = str(row["subject_label"])
                data = {
                    "confidence": float(row["confidence"])
                    if row["confidence"] is not None
                    else 1.0,
                    "object_label": row["object_label"],
                }
                outcomes[subj] = data
                outcomes[subj.removeprefix("PROJECT:")] = data
                outcomes[f"PROJECT:{subj.removeprefix('PROJECT:')}"] = data

    log.info(
        "do_propose_design: ns=%s recalled %d candidate(s) (top_k=%d, attributed_outcomes=%d)",
        ns_uuid,
        len(candidates),
        top_k,
        len(outcomes),
    )

    # 3. Apply outcome-weighting and compute coverage figure.
    ranked, coverage = _apply_outcome_weights(
        candidates,
        enabled=outcome_weighting_enabled,
        outcomes=outcomes,
    )

    if outcome_weighting_enabled and coverage["attributed_outcomes"] == 0 and len(candidates) > 0:
        try:
            from nce.degradation import record_degradation

            record_degradation(
                namespace_id=str(ns_uuid),
                engine="system_design",
                code="similarity_only_zero_outcomes",
                detail=f"Design recall has 0 attributed outcomes for {len(candidates)} candidates.",
                onboarding_hint="Design recall is similarity-only: 0 attributed outcomes; record 5 to unlock.",
            )
        except Exception:
            log.warning("Failed to record degradation event in propose_design", exc_info=True)

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
            "weighted_score": float(r.get("weighted_score", r.get("similarity", 0.0))),
            "attributed_outcome": bool(r.get("attributed_outcome", False)),
            "outcome_confidence": r.get("outcome_confidence"),
        }
        for r in ranked
    ]

    # 6. Fetch Trust Dial autonomy badge (Wave T-1 EU-AI-Act transparency)
    trust_dial_badge: dict[str, Any] | None = None
    try:
        from nce.trust_dial import get_trust_dial_status

        trust_dial_badge = await get_trust_dial_status(
            engine,
            ns_uuid,
            engine="system_design",
        )
    except Exception as exc:
        log.warning("do_propose_design: failed to fetch trust dial badge: %s", exc)

    return {
        "proposed_lines": proposed_lines,
        "recall_evidence": recall_evidence,
        "outcome_weighting_applied": outcome_weighting_enabled
        and (coverage["attributed_outcomes"] > 0),
        "coverage": coverage,
        "trust_dial": trust_dial_badge,
    }
