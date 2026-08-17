"""
nce/vertical_modules/product/matching.py
=========================================
Core: do_match_bom_line — resolve a free-text BOM line to the best catalog SKU
by delegating entirely to the C1 ``resolve()`` primitive.

Design invariants (uncle-bob-craft / dependency rule):
  - Resolution is C1's job: no trigram / fuzzy maths here.  This module
    only builds the candidate dict, selects keys, and interprets the Match list.
  - One function, one job: ``do_match_bom_line`` matches; ``do_record_match_decision``
    writes the learning event.  They are separate concerns.
  - WORM invariant: ``product_match_feedback`` is append-only — this module only
    INSERTs; it never UPDATEs or DELETEs rows.
  - ADR-0017: cost/margin/BID are never returned or logged.
  - Confidence lives on kg_edges only (wave rule 7) — ``Match.score`` is the
    resolver's pg_trgm similarity, surfaced here as ``match_score``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.resolver import resolve
from nce.mcp_args import require_namespace_id

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.product.matching")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: C1 node type for product SKU nodes.
_NODE_TYPE: str = "PRODUCT_SKU"

#: Keys passed to resolve() — match order: manufacturer first, then part number, then name.
_RESOLVE_KEYS: list[str] = ["manufacturer", "mfr_part_no", "name"]

#: Valid decision values for the learning table.
_VALID_DECISIONS: frozenset[str] = frozenset({"accept", "override"})


# ---------------------------------------------------------------------------
# Public core: do_match_bom_line
# ---------------------------------------------------------------------------


async def do_match_bom_line(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a free-text BOM line to the best catalog SKU via C1 resolve().

    Builds a candidate dict from the caller-supplied BOM line fields and
    delegates ranking entirely to ``nce.entity_resolution.resolver.resolve()``.
    Never implements its own fuzzy or trigram matching.

    When ``params`` includes a ``decision`` key ('accept' or 'override') the
    call is treated as a **feedback event**: it records the accept/override
    decision into ``product_match_feedback`` and returns the appended row id.

    Parameters
    ----------
    engine:
        Live NCEEngine instance (provides ``pg_pool``).
    params:
        ``namespace_id``   (str, required)
        ``bom_line``       (str, required)    — raw free-text BOM line.
        ``manufacturer``   (str, optional)    — parsed manufacturer hint.
        ``mfr_part_no``    (str, optional)    — parsed part-number hint.
        ``decision``       (str, optional)    — 'accept' or 'override' (feedback branch).
        ``chosen_sku``     (str, optional)    — the SKU the user accepted/chose (feedback).
        ``rejected_sku``   (str, optional)    — the SKU the user rejected (feedback).
        ``matched_score``  (float, optional)  — the score at decision time (feedback).

    Returns
    -------
    Match branch (no ``decision``):
        ``bom_line``     — the input line.
        ``matches``      — list of {node_id, score, matched_on} dicts, highest first.
        ``top_sku``      — node_id of the highest-scoring match (str) or None.
        ``top_score``    — score of the top match (float) or None.

    Feedback branch (``decision`` present):
        ``feedback_id``  — UUID of the appended ``product_match_feedback`` row (str).
        ``decision``     — the decision that was recorded.
    """
    namespace_id = require_namespace_id(params)

    bom_line = str(params.get("bom_line") or "").strip()
    if not bom_line:
        raise ValueError("'bom_line' is required and must be a non-empty string")

    # -- Feedback branch -------------------------------------------------------
    decision = str(params.get("decision") or "").strip().lower()
    if decision:
        return await do_record_match_decision(engine, params, namespace_id, bom_line, decision)

    # -- Match branch ----------------------------------------------------------
    return await _match(engine, namespace_id, bom_line, params)


async def _match(
    engine: NCEEngine,
    namespace_id: str,
    bom_line: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run C1 resolve() and return ranked matches."""
    candidate: dict[str, str] = {"name": bom_line}

    manufacturer = str(params.get("manufacturer") or "").strip()
    if manufacturer:
        candidate["manufacturer"] = manufacturer

    mfr_part_no = str(params.get("mfr_part_no") or "").strip()
    if mfr_part_no:
        candidate["mfr_part_no"] = mfr_part_no

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        matches = await resolve(
            conn,
            namespace_id=namespace_id,
            candidate=candidate,
            keys=_RESOLVE_KEYS,
            node_type=_NODE_TYPE,
        )

    match_dicts = [
        {
            "node_id": str(m.node_id),
            "score": m.score,
            "matched_on": m.matched_on,
        }
        for m in matches
    ]

    top_sku = str(matches[0].node_id) if matches else None
    top_score = matches[0].score if matches else None

    return {
        "bom_line": bom_line,
        "matches": match_dicts,
        "top_sku": top_sku,
        "top_score": top_score,
    }


# ---------------------------------------------------------------------------
# Public core: do_record_match_decision (feedback / learning event)
# ---------------------------------------------------------------------------


async def do_record_match_decision(
    engine: NCEEngine,
    params: dict[str, Any],
    namespace_id: str,
    bom_line: str,
    decision: str,
) -> dict[str, Any]:
    """Append an accept/override learning event to product_match_feedback.

    This is the write path of the BOM matching loop.  It records which SKU
    the user accepted or overrode so that the resolver can be recalibrated
    over time.  The table is append-only: this function only INSERTs.

    Parameters
    ----------
    engine:
        Live NCEEngine instance.
    params:
        Full params dict from the MCP call.
    namespace_id:
        Already-validated namespace UUID string.
    bom_line:
        The free-text BOM line that was matched.
    decision:
        'accept' or 'override' (already stripped/lower-cased by the caller).

    Returns
    -------
    dict with ``feedback_id`` and ``decision``.

    Raises
    ------
    ValueError:
        If ``decision`` is not 'accept' or 'override'.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(
            f"'decision' must be one of {sorted(_VALID_DECISIONS)!r}, got {decision!r}"
        )

    chosen_sku = str(params.get("chosen_sku") or "").strip() or None
    rejected_sku = str(params.get("rejected_sku") or "").strip() or None

    raw_score = params.get("matched_score")
    matched_score: float | None = None
    if raw_score is not None:
        try:
            matched_score = float(raw_score)
        except (TypeError, ValueError):
            matched_score = None

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        feedback_id = await conn.fetchval(
            """
            INSERT INTO product_match_feedback
                (namespace_id, bom_line, chosen_sku, rejected_sku, decision, matched_score)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            UUID(str(namespace_id)),
            bom_line,
            chosen_sku,
            rejected_sku,
            decision,
            matched_score,
        )

    log.info(
        "do_record_match_decision: namespace=%s decision=%s feedback_id=%s",
        namespace_id,
        decision,
        feedback_id,
    )

    return {"feedback_id": str(feedback_id), "decision": decision}
