"""
nce/vertical_modules/procurement/recalibration.py
==================================================
Ledger-backed per-supplier recalibration (Module 1 Wave 8).

``do_record_match_decision`` appends each match outcome to
``v3_cognitive_ledger`` (append-only INSERT; never UPDATE).

``do_recalibrate_supplier`` reads the rolling N-decision window for one
supplier, derives a precision-earned weight/threshold delta, and returns it
for the caller to apply.  The derivation reads ONLY ledger rows so an
auditor can reconstruct every movement from the table alone.

Design invariants
-----------------
- No new table.  Learning lives in ``v3_cognitive_ledger``.
- ``tlx_scores`` (jsonb) stores the match-decision payload so it is
  queryable alongside the other cognitive-ledger metadata already written
  by the product ingestion path (see nce/vertical_modules/product/ingestion.py).
- ``memory_id`` is nullable in the live schema; we set it to NULL here
  (no associated memories row — this row represents a procurement event,
  not a product spec).
- ``empathic_tensor`` has a NOT NULL constraint; we store a zero vector
  (6 floats) matching the B39 ingestion pattern.
- ``model_version`` is NOT NULL; we use ``"procurement-recal-1.0"``.
- Threshold movement is EARNED from measured precision (§9.3): the delta is
  ``(precision - 0.5) × 0.1`` clamped to [-0.05, +0.05].  A supplier that
  is accepted 90 % of the time (precision 0.9) earns +0.04 towards its
  auto-accept threshold; one accepted only 40 % earns -0.01 (threshold
  tightens).
- Recalibration fires only when a supplier has >= NCE_PROCUREMENT_RECALIBRATE_AFTER_N
  decisions.  Below N the function returns ``{"recalibrated": False, ...}``.
- ``scoped_pg_session`` + ``namespace_id`` enforce RLS on every write/read.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.procurement.recalibration")

# Agent label written into v3_cognitive_ledger.model_version for procurement rows.
_MODEL_VERSION = "procurement-recal-1.0"

# Zero tensor matching the NOT NULL empathic_tensor column (float[6] in the live schema).
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# Precision midpoint — suppliers above this earn a positive threshold delta.
_PRECISION_MIDPOINT: float = 0.5

# Maximum absolute delta applied per recalibration window.  Keeps movement
# conservative: earned from data, not from a single lucky batch.
_MAX_DELTA: float = 0.05

# Scale factor mapping (precision - 0.5) → delta.
# At precision=1.0 → delta = 0.5 × 0.1 = +0.05 (capped).
# At precision=0.0 → delta = -0.5 × 0.1 = -0.05 (capped).
_DELTA_SCALE: float = 0.1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def do_record_match_decision(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    supplier_id: str,
    decision: str,
    score: float,
) -> dict[str, Any]:
    """Append one match-outcome row to ``v3_cognitive_ledger`` (append-only).

    Parameters
    ----------
    pg_pool:
        asyncpg connection pool.  RLS context is set inside ``scoped_pg_session``.
    namespace_id:
        Tenant namespace UUID — all writes are scoped to this namespace.
    supplier_id:
        Identifier of the supplier being evaluated.
    decision:
        ``"accept"`` — the match was accepted automatically.
        ``"override"`` — a human overrode the system recommendation.
    score:
        Confidence score (0.0–100.0) from ``do_evaluate_three_way_match`` at
        decision time.

    Returns
    -------
    dict with ``ledger_id`` (str UUID of the inserted row) and ``supplier_id``.
    """
    if decision not in ("accept", "override"):
        raise ValueError(f"decision must be 'accept' or 'override', got {decision!r}")

    ledger_id = uuid.uuid4()

    payload: dict[str, Any] = {
        "event_type": "match_decision",
        "supplier_id": supplier_id,
        "decision": decision,
        "score": score,
    }

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
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
            str(namespace_id),
            _ZERO_TENSOR,
            json.dumps(payload),
            json.dumps({}),
            _MODEL_VERSION,
        )

    log.info(
        "[PROCUREMENT-RECAL] decision recorded supplier_id=%s decision=%s score=%.2f ledger_id=%s",
        supplier_id,
        decision,
        score,
        ledger_id,
    )
    return {"ledger_id": str(ledger_id), "supplier_id": supplier_id}


async def do_recalibrate_supplier(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    supplier_id: str,
    window_n: int,
) -> dict[str, Any]:
    """Derive a precision-earned threshold/weight delta for one supplier.

    Reads the last ``window_n`` match-decision rows from ``v3_cognitive_ledger``
    for ``supplier_id`` in this namespace, computes precision (fraction of
    accepted decisions), and returns the delta.

    The derivation is fully reconstructable from the ledger: an auditor running
    the same query over the same rows will arrive at the same delta.

    Parameters
    ----------
    pg_pool:
        asyncpg connection pool.
    namespace_id:
        Tenant namespace UUID (RLS enforced).
    supplier_id:
        Which supplier to recalibrate.
    window_n:
        Rolling window size (``NCE_PROCUREMENT_RECALIBRATE_AFTER_N``).
        When the supplier has fewer than ``window_n`` decisions,
        the function returns early without computing a delta.

    Returns
    -------
    dict with:
      ``recalibrated``   bool — True when a delta was computed.
      ``supplier_id``    str
      ``decision_count`` int  — decisions found in the rolling window.
      ``precision``      float | None — fraction accepted (or None when skipped).
      ``threshold_delta`` float | None — signed delta to apply (or None when skipped).
      ``weight_delta``    float | None — mirrors threshold_delta (same magnitude,
                          separate key for consumers that track weights vs thresholds
                          independently).
    """
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        rows = await conn.fetch(
            """
            SELECT tlx_scores
            FROM   v3_cognitive_ledger
            WHERE  namespace_id = $1::uuid
              AND  tlx_scores->>'event_type' = 'match_decision'
              AND  tlx_scores->>'supplier_id' = $2
            ORDER BY created_at DESC
            LIMIT  $3
            """,
            str(namespace_id),
            supplier_id,
            window_n,
        )

    decision_count = len(rows)

    if decision_count < window_n:
        log.debug(
            "[PROCUREMENT-RECAL] skip supplier_id=%s decisions=%d < window=%d",
            supplier_id,
            decision_count,
            window_n,
        )
        return {
            "recalibrated": False,
            "supplier_id": supplier_id,
            "decision_count": decision_count,
            "precision": None,
            "threshold_delta": None,
            "weight_delta": None,
        }

    accepted = sum(
        1
        for r in rows
        if (
            json.loads(r["tlx_scores"]) if isinstance(r["tlx_scores"], str) else r["tlx_scores"]
        ).get("decision")
        == "accept"
    )
    precision = accepted / decision_count

    # Threshold movement earned from measured precision (§9.3).
    # delta in [-MAX_DELTA, +MAX_DELTA]; positive → relax threshold (supplier earns trust).
    raw_delta = (precision - _PRECISION_MIDPOINT) * _DELTA_SCALE
    threshold_delta = max(-_MAX_DELTA, min(_MAX_DELTA, raw_delta))

    log.info(
        "[PROCUREMENT-RECAL] recalibrated supplier_id=%s precision=%.3f delta=%.4f window=%d",
        supplier_id,
        precision,
        threshold_delta,
        window_n,
    )
    return {
        "recalibrated": True,
        "supplier_id": supplier_id,
        "decision_count": decision_count,
        "precision": precision,
        "threshold_delta": threshold_delta,
        "weight_delta": threshold_delta,
    }
