"""
nce/vertical_modules/procurement/frontier.py
=============================================
Frontier-AI Advisor capabilities — Module 1 Wave 12.

Three pure-calc functions (no DB/web import) + three DB wrapper handlers.

Advisor discipline
------------------
All three functions RECOMMEND only.  They never call ``submit_po``,
``generate_po``, or write any procurement row.  A human (or a separately
C2-governed ``submit_po``) decides.  STOP if tempted to add a PO write.

Pure cores (zero infrastructure)
---------------------------------
``forecast_rebate(bom_rows, kickback_tiers)``
    Projects year-end rebate based on forecast annual spend vs supplier
    kickback tiers.  Formula::

        annual_spend   = sum(unit_price × quantity for each bom_row)
        matched_tier   = highest tier where annual_spend >= tier["min_spend"]
        rebate_amount  = annual_spend × matched_tier["rate"]  (or 0 if no tier)
        rebate_band    = (rebate_amount × 0.9, rebate_amount × 1.1)  # ±10 % band

    Returns ``{annual_spend, matched_tier, rebate_amount, rebate_low, rebate_high,
               confidence, rationale}``.

``recommend_move_spend(suppliers)``
    Ranks suppliers by ROI signal = precision (from recalibration) × (1 - lost_rate).
    ``lost_rate`` = lost_savings / realised_savings if realised > 0 else 0.
    Returns the top supplier recommendation + rationale.

``simulate_whatif_spend(current_spend, shift_fraction, from_supplier, to_supplier)``
    Deterministic: given a spend-shift fraction, returns projected savings/rebate
    delta.  Formula::

        shifted_spend   = current_spend × shift_fraction
        delta_savings   = shifted_spend × (to_supplier["margin_rate"]
                          - from_supplier["margin_rate"])
        delta_rebate    = shifted_spend × (to_supplier["rebate_rate"]
                          - from_supplier["rebate_rate"])
        net_delta       = delta_savings + delta_rebate

DB wrappers (read-only; degrade gracefully when forward-ref tables absent)
--------------------------------------------------------------------------
``do_forecast_rebate(engine, params)``
    Reads BOM pipeline rows from ``procurement_bom_pipeline`` (forward-ref;
    degrades to empty if absent) and kickback tiers from
    ``procurement_kickback_tiers`` (degrades to empty if absent).

``do_recommend_move_spend(engine, params)``
    Reads per-supplier recalibration stats from ``v3_cognitive_ledger`` (W8)
    and savings aggregates from ``procurement_spend_lines`` (W9, may be absent).

``do_whatif_spend(engine, params)``
    Reads per-supplier spend summary from ``procurement_spend_lines`` and
    kickback / margin rate metadata.  Degrades gracefully when tables absent.

WORM / RLS invariants
---------------------
- All SQL runs inside ``scoped_pg_session`` (namespace_id filter enforced).
- No INSERT, UPDATE, DELETE anywhere in this module.
- ``confidence`` (0–1) lives on ``kg_edges`` only — not used here.
- Never UPDATE/DELETE ``event_log``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.procurement.frontier")

# ±10 % confidence band around point estimate.
_REBATE_BAND_FACTOR_LOW: float = 0.9
_REBATE_BAND_FACTOR_HIGH: float = 1.1


# ---------------------------------------------------------------------------
# Pure calculation cores — zero DB, zero HTTP
# ---------------------------------------------------------------------------


def forecast_rebate(
    bom_rows: list[dict[str, Any]],
    kickback_tiers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project year-end rebate band from BOM pipeline rows + kickback tiers.

    Parameters
    ----------
    bom_rows:
        List of dicts with ``unit_price`` (float) and ``quantity`` (float/int).
        Forward-ref table — may be empty; returns neutral recommendation.
    kickback_tiers:
        List of dicts with ``min_spend`` (float) and ``rate`` (float, 0–1).
        Sorted descending by ``min_spend`` internally.

    Returns
    -------
    dict with:
        ``annual_spend``  — float, projected annual spend across all BOM rows.
        ``matched_tier``  — dict | None, highest tier matched (or None).
        ``rebate_amount`` — float, point-estimate rebate (0 if no tier matched).
        ``rebate_low``    — float, lower bound of ±10 % band.
        ``rebate_high``   — float, upper bound of ±10 % band.
        ``confidence``    — str, "low" when no data, "medium" when tiers present.
        ``rationale``     — str, human-readable explanation.
    """
    annual_spend: float = sum(
        float(r.get("unit_price", 0.0)) * float(r.get("quantity", 0.0)) for r in bom_rows
    )

    if not bom_rows:
        return {
            "annual_spend": 0.0,
            "matched_tier": None,
            "rebate_amount": 0.0,
            "rebate_low": 0.0,
            "rebate_high": 0.0,
            "confidence": "low",
            "rationale": "No BOM pipeline data available — rebate forecast is not possible yet.",
        }

    # Highest tier where annual_spend >= tier["min_spend"].
    sorted_tiers = sorted(kickback_tiers, key=lambda t: float(t["min_spend"]), reverse=True)
    matched_tier: dict[str, Any] | None = None
    for tier in sorted_tiers:
        if annual_spend >= float(tier["min_spend"]):
            matched_tier = tier
            break

    rebate_amount: float = 0.0
    if matched_tier is not None:
        rebate_amount = annual_spend * float(matched_tier["rate"])

    rebate_low = rebate_amount * _REBATE_BAND_FACTOR_LOW
    rebate_high = rebate_amount * _REBATE_BAND_FACTOR_HIGH
    confidence = "medium" if matched_tier else "low"

    if matched_tier:
        rationale = (
            f"Projected annual spend {annual_spend:.2f} qualifies for tier "
            f"'{matched_tier.get('name', matched_tier.get('min_spend', '?'))}' "
            f"(rate {matched_tier['rate']:.2%}), yielding a rebate estimate of "
            f"{rebate_amount:.2f} (±10 % band: {rebate_low:.2f}–{rebate_high:.2f})."
        )
    else:
        rationale = (
            f"Projected annual spend {annual_spend:.2f} does not reach any kickback "
            f"tier minimum.  No rebate expected at current run-rate."
        )

    return {
        "annual_spend": annual_spend,
        "matched_tier": matched_tier,
        "rebate_amount": rebate_amount,
        "rebate_low": rebate_low,
        "rebate_high": rebate_high,
        "confidence": confidence,
        "rationale": rationale,
    }


def recommend_move_spend(
    suppliers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Recommend which supplier to consolidate spend toward for best ROI.

    ROI signal per supplier::

        roi_score = precision × (1 - lost_rate)
        lost_rate = lost / realised  if realised > 0  else  0.0

    ``precision``  — fraction of accepted match decisions (from W8 recalibration,
                      0–1).  Defaults to 0.5 when absent.
    ``realised``   — total savings realised (from W9 aggregate).
    ``lost``       — total savings lost (overspend vs baseline).

    Parameters
    ----------
    suppliers:
        List of dicts with:
          ``supplier_id``  (str)
          ``precision``    (float, optional, default 0.5)
          ``realised``     (float, optional, default 0.0)
          ``lost``         (float, optional, default 0.0)

    Returns
    -------
    dict with:
        ``recommendation`` — str, "move_spend" or "no_data".
        ``top_supplier``   — dict | None, highest-ROI supplier entry.
        ``roi_scores``     — list[dict], all suppliers with their roi_score.
        ``rationale``      — str.
    """
    if not suppliers:
        return {
            "recommendation": "no_data",
            "top_supplier": None,
            "roi_scores": [],
            "rationale": "No supplier data available for ROI recommendation.",
        }

    scored: list[dict[str, Any]] = []
    for s in suppliers:
        precision = float(s.get("precision") or 0.5)
        realised = float(s.get("realised") or 0.0)
        lost = float(s.get("lost") or 0.0)
        lost_rate = (lost / realised) if realised > 0 else 0.0
        roi_score = precision * (1.0 - lost_rate)
        scored.append({**s, "roi_score": roi_score, "lost_rate": lost_rate})

    scored.sort(key=lambda x: x["roi_score"], reverse=True)
    top = scored[0]

    rationale = (
        f"Supplier '{top['supplier_id']}' has the highest ROI score "
        f"{top['roi_score']:.3f} (precision={top.get('precision', 0.5):.3f}, "
        f"lost_rate={top['lost_rate']:.3f}).  "
        "Recommend consolidating discretionary spend toward this supplier. "
        "A human or C2-governed process must approve any resulting PO."
    )

    return {
        "recommendation": "move_spend",
        "top_supplier": top,
        "roi_scores": scored,
        "rationale": rationale,
    }


def simulate_whatif_spend(
    current_spend: float,
    shift_fraction: float,
    from_supplier: dict[str, Any],
    to_supplier: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic what-if: shift a fraction of spend and return projected delta.

    Formula::

        shifted_spend = current_spend × shift_fraction
        delta_savings = shifted_spend × (to["margin_rate"] - from["margin_rate"])
        delta_rebate  = shifted_spend × (to["rebate_rate"] - from["rebate_rate"])
        net_delta     = delta_savings + delta_rebate

    Parameters
    ----------
    current_spend:
        Total spend subject to reallocation (currency units).
    shift_fraction:
        Fraction to shift, 0.0–1.0.
    from_supplier:
        Dict with ``supplier_id``, ``margin_rate`` (float), ``rebate_rate`` (float).
    to_supplier:
        Dict with ``supplier_id``, ``margin_rate`` (float), ``rebate_rate`` (float).

    Returns
    -------
    dict with:
        ``shifted_spend``   — float, spend moved.
        ``delta_savings``   — float, savings delta (positive = gain).
        ``delta_rebate``    — float, rebate delta.
        ``net_delta``       — float, total projected gain/loss.
        ``recommendation``  — str, "move" if net_delta > 0 else "hold".
        ``rationale``       — str.
    """
    shift_fraction = max(0.0, min(1.0, shift_fraction))
    shifted_spend = current_spend * shift_fraction

    from_margin = float(from_supplier.get("margin_rate") or 0.0)
    to_margin = float(to_supplier.get("margin_rate") or 0.0)
    from_rebate = float(from_supplier.get("rebate_rate") or 0.0)
    to_rebate = float(to_supplier.get("rebate_rate") or 0.0)

    delta_savings = shifted_spend * (to_margin - from_margin)
    delta_rebate = shifted_spend * (to_rebate - from_rebate)
    net_delta = delta_savings + delta_rebate

    recommendation = "move" if net_delta > 0 else "hold"

    rationale = (
        f"Shifting {shift_fraction:.0%} of {current_spend:.2f} "
        f"({shifted_spend:.2f}) from '{from_supplier.get('supplier_id')}' "
        f"to '{to_supplier.get('supplier_id')}' projects a net delta of "
        f"{net_delta:.2f} (savings Δ {delta_savings:.2f}, rebate Δ {delta_rebate:.2f}).  "
        f"Recommendation: {recommendation.upper()}.  "
        "No PO is written — a human or C2-governed process decides."
    )

    return {
        "shifted_spend": shifted_spend,
        "delta_savings": delta_savings,
        "delta_rebate": delta_rebate,
        "net_delta": net_delta,
        "recommendation": recommendation,
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# DB read helpers — to_regclass graceful-degrade pattern (W9 precedent)
# ---------------------------------------------------------------------------


async def _fetch_bom_pipeline(
    conn: Any,
    namespace_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Read BOM pipeline rows.  Returns [] if table not yet created."""
    exists = await conn.fetchval("SELECT to_regclass('public.procurement_bom_pipeline')")
    if exists is None:
        log.info(
            "[procurement-frontier] procurement_bom_pipeline absent — returning empty BOM rows"
        )
        return []
    rows = await conn.fetch(
        """
        SELECT unit_price, quantity, supplier_id
        FROM   procurement_bom_pipeline
        WHERE  namespace_id = $1
        ORDER  BY supplier_id
        """,
        namespace_id,
    )
    return [dict(r) for r in rows]


async def _fetch_kickback_tiers(
    conn: Any,
    namespace_id: uuid.UUID,
    supplier_id: str | None,
) -> list[dict[str, Any]]:
    """Read kickback tier definitions.  Returns [] if table absent."""
    exists = await conn.fetchval("SELECT to_regclass('public.procurement_kickback_tiers')")
    if exists is None:
        log.info("[procurement-frontier] procurement_kickback_tiers absent — returning empty tiers")
        return []
    if supplier_id:
        rows = await conn.fetch(
            """
            SELECT name, min_spend, rate
            FROM   procurement_kickback_tiers
            WHERE  namespace_id = $1
              AND  supplier_id  = $2
            ORDER  BY min_spend DESC
            """,
            namespace_id,
            supplier_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT name, min_spend, rate
            FROM   procurement_kickback_tiers
            WHERE  namespace_id = $1
            ORDER  BY min_spend DESC
            """,
            namespace_id,
        )
    return [dict(r) for r in rows]


async def _fetch_supplier_recal_stats(
    conn: Any,
    namespace_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Read per-supplier precision from v3_cognitive_ledger (W8 match decisions)."""
    rows = await conn.fetch(
        """
        SELECT
            tlx_scores->>'supplier_id'                                    AS supplier_id,
            COUNT(*)::bigint                                               AS decision_count,
            SUM(CASE WHEN tlx_scores->>'decision' = 'accept' THEN 1 ELSE 0 END)::bigint
                                                                           AS accepted
        FROM   v3_cognitive_ledger
        WHERE  namespace_id = $1::uuid
          AND  tlx_scores->>'event_type' = 'match_decision'
        GROUP  BY tlx_scores->>'supplier_id'
        """,
        str(namespace_id),
    )
    result = []
    for r in rows:
        count = int(r["decision_count"])
        accepted = int(r["accepted"])
        precision = (accepted / count) if count > 0 else 0.5
        result.append({"supplier_id": r["supplier_id"], "precision": precision})
    return result


async def _fetch_supplier_savings_stats(
    conn: Any,
    namespace_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Read per-supplier spend totals from procurement_spend_lines.  Degrades if absent."""
    exists = await conn.fetchval("SELECT to_regclass('public.procurement_spend_lines')")
    if exists is None:
        log.info(
            "[procurement-frontier] procurement_spend_lines absent — returning empty savings stats"
        )
        return []
    rows = await conn.fetch(
        """
        SELECT
            leverandor                               AS supplier_id,
            SUM(unit_price * quantity)::float        AS total_spend
        FROM   procurement_spend_lines
        WHERE  namespace_id = $1
        GROUP  BY leverandor
        """,
        namespace_id,
    )
    return [{"supplier_id": r["supplier_id"], "total_spend": float(r["total_spend"])} for r in rows]


async def _fetch_supplier_margin_rebate_rates(
    conn: Any,
    namespace_id: uuid.UUID,
) -> dict[str, dict[str, float]]:
    """Read margin_rate + rebate_rate from procurement_kickback_tiers.  Degrades if absent."""
    exists = await conn.fetchval("SELECT to_regclass('public.procurement_kickback_tiers')")
    if exists is None:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (supplier_id)
               supplier_id,
               COALESCE(margin_rate, 0.0)::float  AS margin_rate,
               COALESCE(rate,        0.0)::float  AS rebate_rate
        FROM   procurement_kickback_tiers
        WHERE  namespace_id = $1
        ORDER  BY supplier_id, min_spend DESC
        """,
        namespace_id,
    )
    return {
        r["supplier_id"]: {
            "margin_rate": float(r["margin_rate"]),
            "rebate_rate": float(r["rebate_rate"]),
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# Public Advisor handlers
# ---------------------------------------------------------------------------


async def do_forecast_rebate(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Advisor: forecast year-end rebate band from BOM pipeline + kickback tiers.

    Parameters
    ----------
    engine:
        Live NCEEngine instance (provides ``pg_pool``).
    params:
        ``namespace_id`` (str, UUID) — required.
        ``supplier_id``  (str)       — optional; filter BOM rows + tiers to one supplier.

    Returns
    -------
    dict — see ``forecast_rebate`` for shape.

    Notes
    -----
    Read-only Advisor.  No PO write, no INSERT/UPDATE/DELETE.
    Degrades gracefully when ``procurement_bom_pipeline`` or
    ``procurement_kickback_tiers`` are not yet created.
    """
    raw_ns = require_namespace_id(params)
    namespace_id = uuid.UUID(raw_ns)
    supplier_id: str | None = params.get("supplier_id") or None

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        bom_rows = await _fetch_bom_pipeline(conn, namespace_id)
        if supplier_id:
            bom_rows = [r for r in bom_rows if r.get("supplier_id") == supplier_id]
        tiers = await _fetch_kickback_tiers(conn, namespace_id, supplier_id)

    result = forecast_rebate(bom_rows, tiers)
    log.info(
        "[procurement-frontier] forecast_rebate namespace=%s supplier=%s annual_spend=%.2f",
        namespace_id,
        supplier_id,
        result["annual_spend"],
    )
    return result


async def do_recommend_move_spend(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Advisor: recommend which supplier to consolidate spend toward for best ROI.

    Combines W8 recalibration precision stats with W9 savings aggregates.

    Parameters
    ----------
    engine:
        Live NCEEngine instance.
    params:
        ``namespace_id`` (str, UUID) — required.

    Returns
    -------
    dict — see ``recommend_move_spend`` for shape.

    Notes
    -----
    Read-only Advisor.  No PO write anywhere in this call chain.
    """
    raw_ns = require_namespace_id(params)
    namespace_id = uuid.UUID(raw_ns)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        recal_stats = await _fetch_supplier_recal_stats(conn, namespace_id)
        savings_stats = await _fetch_supplier_savings_stats(conn, namespace_id)

    # Merge by supplier_id
    savings_by_id: dict[str, dict[str, Any]] = {s["supplier_id"]: s for s in savings_stats}
    merged: list[dict[str, Any]] = []
    for r in recal_stats:
        sid = r["supplier_id"]
        sav = savings_by_id.get(sid, {})
        merged.append(
            {
                "supplier_id": sid,
                "precision": r["precision"],
                "realised": sav.get("realised", 0.0),
                "lost": sav.get("lost", 0.0),
            }
        )
    # Also include suppliers with savings data but no recal stats
    for sid, sav in savings_by_id.items():
        if not any(m["supplier_id"] == sid for m in merged):
            merged.append(
                {
                    "supplier_id": sid,
                    "precision": 0.5,
                    "realised": sav.get("realised", 0.0),
                    "lost": sav.get("lost", 0.0),
                }
            )

    result = recommend_move_spend(merged)
    log.info(
        "[procurement-frontier] recommend_move_spend namespace=%s recommendation=%s",
        namespace_id,
        result["recommendation"],
    )
    return result


async def do_whatif_spend(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Advisor: simulate a hypothetical spend shift and return projected delta.

    Parameters
    ----------
    engine:
        Live NCEEngine instance.
    params:
        ``namespace_id``   (str, UUID) — required.
        ``from_supplier``  (str)       — supplier_id to shift spend away from.
        ``to_supplier``    (str)       — supplier_id to shift spend toward.
        ``shift_fraction`` (float)     — fraction of current spend to shift (0–1).

    Returns
    -------
    dict — see ``simulate_whatif_spend`` for shape.

    Notes
    -----
    Read-only Advisor.  Deterministic given the same inputs.
    No PO write.  Degrades gracefully when spend/tier tables absent.
    """
    raw_ns = require_namespace_id(params)
    namespace_id = uuid.UUID(raw_ns)

    from_id: str = str(params.get("from_supplier") or "")
    to_id: str = str(params.get("to_supplier") or "")
    shift_fraction: float = float(params.get("shift_fraction") or 0.0)

    if not from_id:
        raise ValueError("do_whatif_spend: 'from_supplier' is required")
    if not to_id:
        raise ValueError("do_whatif_spend: 'to_supplier' is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        spend_stats = await _fetch_supplier_savings_stats(conn, namespace_id)
        rates = await _fetch_supplier_margin_rebate_rates(conn, namespace_id)

    spend_by_id: dict[str, float] = {s["supplier_id"]: s["total_spend"] for s in spend_stats}
    current_spend = spend_by_id.get(from_id, 0.0)

    from_rates = rates.get(from_id, {"margin_rate": 0.0, "rebate_rate": 0.0})
    to_rates = rates.get(to_id, {"margin_rate": 0.0, "rebate_rate": 0.0})

    from_supplier = {"supplier_id": from_id, **from_rates}
    to_supplier = {"supplier_id": to_id, **to_rates}

    result = simulate_whatif_spend(current_spend, shift_fraction, from_supplier, to_supplier)
    log.info(
        "[procurement-frontier] whatif_spend namespace=%s from=%s to=%s shift=%.2f net_delta=%.2f",
        namespace_id,
        from_id,
        to_id,
        shift_fraction,
        result["net_delta"],
    )
    return result
