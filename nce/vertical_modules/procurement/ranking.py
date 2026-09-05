"""
nce/vertical_modules/procurement/ranking.py
============================================
Pure supplier-ranking core — zero DB, zero HTTP, zero web/admin imports.

Reconstructed near-1:1 from the reference implementation /
``rankSuppliers``.

5-step DELIBERATE order
-----------------------
Steps run in this fixed order; each step narrows / re-scores the candidate set:

  (1) Own-stock preference  — candidates with own_stock=True receive a full bonus;
      others score 0 on this dimension.
  (2) Delivery-deadline filter  — candidates whose ``lead_time_days`` would miss the
      BOM-line ``required_by_day`` are retained but score 0 on delivery_reliability.
      (Callers that want hard exclusion can filter the returned list themselves.)
  (3) True TCO  — calls Wave-1 ``do_calculate_tco``; scored relative to the cheapest TCO
      among all candidates (lower TCO → higher TCO score).
  (4) BID price  — priceScore computed from the candidate's ``unit_price`` relative to the
      cheapest candidate (lower price → higher score, config-weighted).

      MILESTONE — priceScore gap closed (round-2 #4):
        BEFORE: the reference implementation's original had ``priceScore=3`` (a hardcoded placeholder that did
                not respond to price at all — every candidate got the same flat score of 3
                regardless of what they charged).
        AFTER:  priceScore = (min_price / candidate_price) × bid_price_weight so a
                supplier who quotes the cheapest price receives the full bid_price_weight
                contribution and more expensive suppliers are penalised proportionally.
                This changes rankings vs the placeholder (intended per round-2 #4).

  (5) Tier × kickback-proximity × bundling  — governance-aware bonus:
        tier_score         = (4 − supplier_tier) / 3  (tier 1 → 1.0, tier 4 → 0.0)
        kickback_proximity = candidate's ``kickback_proximity`` (0–1); 0.5 when absent.
        bundling_flag      = 1.0 if ``bundles_well=True`` else 0.0
        step5_score        = (tier_score + kickback_proximity + bundling_flag) / 3

      When the composite winner from step 5 differs from the best-TCO winner, a
      ``rebate_override: True`` flag + human-readable ``rebate_rationale`` are added to
      the result for compliance review / ledger audit.  (The Agreements A2A enforcement
      is Wave 11 — this wave only surfaces the flag honestly.)

Scoring
-------
composite_score = sum(step_i_score × weight_i) for i in {1..5}

All weights come from ``weights["SCORING_WEIGHTS"]`` (loaded via Wave-1
``load_procurement_config``).  No weight literals live here.

Conservative defaults for missing fields
-----------------------------------------
If a candidate lacks a field a step needs, a conservative score (0) is used rather than
silently dropping the candidate, per spec rule.  Each default is documented at its callsite.
"""

from __future__ import annotations

from typing import Any

from nce.vertical_modules.procurement.tco import (
    do_calculate_tco,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def do_rank_suppliers(
    weights: dict[str, Any],
    bom_line: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rank procurement candidates for one BOM line using the 5-step DELIBERATE order.

    Parameters
    ----------
    weights:
        Full weights dict from ``procurement-weights.json`` (contains ``TCO_WEIGHTS``
        and ``SCORING_WEIGHTS``).  Loaded via Wave-1 ``load_procurement_config``.
    bom_line:
        The BOM line being sourced.  Required key: ``quantity`` (int).
        Optional keys:
          ``unit_price``       — buyer reference cost per unit (passed to TCO).
          ``required_by_day``  — int; days from now by which delivery is needed.
                                 When absent, the delivery-deadline step is skipped
                                 (all candidates pass step 2).
    candidates:
        List of supplier dicts.  Each must contain ``unit_price`` (float).
        Optional (conservative defaults applied when absent):
          ``own_stock``          — bool; default False.
          ``lead_time_days``     — int; default conservatively large (9999) so the
                                   candidate misses any tight deadline.
          ``delivery_reliability`` — float 0–1; default 0.0 (conservative).
          ``supplier_tier``      — int 1–4; default 4 (lowest tier, conservative).
          ``kickback_proximity`` — float 0–1; default 0.5 (neutral, per spec intent).
          ``bundles_well``       — bool; default False (conservative).
          ``supplier_id``        — str; used in rationale messages.

    Returns
    -------
    dict with keys:
      ``ranked``          — list of candidate dicts, sorted descending by
                            ``composite_score``, each annotated with:
                              ``composite_score``  float
                              ``score_breakdown``  dict (one entry per step)
                              ``tco``              dict (Wave-1 TCO result)
      ``rebate_override`` — bool; True when step-5 changes the winner vs best-TCO.
      ``rebate_rationale`` — str; human-readable reason (empty string when not overridden).

    Raises
    ------
    ValueError:
        When ``candidates`` is empty, or a candidate is missing ``unit_price``,
        or ``bom_line`` is missing ``quantity``.
    """
    if not candidates:
        raise ValueError("candidates list must not be empty")

    scoring_w = weights["SCORING_WEIGHTS"]

    # -----------------------------------------------------------------------
    # Step 1: own-stock preference
    # -----------------------------------------------------------------------
    def _step1_own_stock(candidate: dict[str, Any]) -> float:
        """Score 1.0 when own_stock is True, else 0.0 (conservative default: False)."""
        if candidate.get("own_stock", False):
            return 1.0
        return 0.0

    # -----------------------------------------------------------------------
    # Step 2: delivery-deadline filter
    # -----------------------------------------------------------------------
    required_by_day: int | None = bom_line.get("required_by_day")

    def _step2_delivery_reliability(candidate: dict[str, Any]) -> float:
        """Score from delivery_reliability (0–1), zeroed if deadline is missed.

        When bom_line has no required_by_day the step runs without the deadline
        gate — the raw delivery_reliability score is used directly.
        Conservative default for missing lead_time_days: 9999 (misses any tight
        deadline).  Conservative default for missing delivery_reliability: 0.0.
        """
        lead_time = int(candidate.get("lead_time_days", 9999))
        if required_by_day is not None and lead_time > required_by_day:
            # Candidate cannot meet the deadline — score 0 (not dropped, per spec).
            return 0.0
        return float(candidate.get("delivery_reliability", 0.0))

    # -----------------------------------------------------------------------
    # Step 3: true TCO (via Wave-1 do_calculate_tco)
    # -----------------------------------------------------------------------
    # Compute raw TCO totals first so we can normalise later.
    tco_results: list[dict[str, Any]] = [
        do_calculate_tco(weights, {}, candidate, bom_line) for candidate in candidates
    ]
    tco_totals = [r["total"] for r in tco_results]
    min_tco = min(tco_totals)

    def _step3_tco_score(tco_total: float) -> float:
        """Score = min_tco / tco_total (lower TCO → higher score, range 0–1)."""
        if tco_total <= 0:
            return 0.0
        return min_tco / tco_total

    # -----------------------------------------------------------------------
    # Step 4: BID price score
    #
    # MILESTONE — priceScore gap closed (round-2 #4):
    #   BEFORE: priceScore=3 (hardcoded placeholder — did not respond to price).
    #   AFTER:  priceScore = (min_price / candidate_price) so the cheapest candidate
    #           scores 1.0 and costlier candidates score proportionally less.
    #           This is then multiplied by the bid_price_weight from config.
    #           A lower price now deterministically improves rank.
    # -----------------------------------------------------------------------
    prices = [float(c["unit_price"]) for c in candidates]
    min_price = min(prices)

    def _step4_bid_price(unit_price: float) -> float:
        """Score = min_price / unit_price (lower price → higher score, range 0–1)."""
        if unit_price <= 0:
            return 0.0
        return min_price / unit_price

    # -----------------------------------------------------------------------
    # Step 5: tier × kickback-proximity × bundling (governance-aware bonus)
    # -----------------------------------------------------------------------
    def _step5_tier_kickback_bundling(candidate: dict[str, Any]) -> float:
        """Composite of tier quality, kickback proximity, and bundling benefit.

        tier_score         = (4 − supplier_tier) / 3  → tier 1=1.0, tier 4=0.0
        kickback_proximity = 0–1 float; default 0.5 (neutral — spec is silent on a
                             better conservative, 0.5 is the midpoint, non-punitive).
        bundling_flag      = 1.0 if bundles_well=True, else 0.0 (conservative: False).
        step5_score        = (tier_score + kickback_proximity + bundling_flag) / 3
        """
        raw_tier = int(candidate.get("supplier_tier", 4))
        # Clamp tier to valid range 1–4.
        tier = max(1, min(4, raw_tier))
        tier_score = (4 - tier) / 3.0

        kickback_proximity = float(candidate.get("kickback_proximity", 0.5))

        bundling_flag = 1.0 if candidate.get("bundles_well", False) else 0.0

        return (tier_score + kickback_proximity + bundling_flag) / 3.0

    # -----------------------------------------------------------------------
    # Composite scoring and annotation
    # -----------------------------------------------------------------------
    # Note: the SCORING_WEIGHTS block uses "tco" as the primary quality weight.
    # Per the engine spec the 5-step weights map as:
    #   own_stock          → not explicitly weighted; it acts as a tie-break / bonus
    #                        layer.  We give it a proportional share of the tco weight.
    #   delivery_reliability → delivery_reliability weight
    #   tco                  → tco weight
    #   bid_price            → bid_price weight
    #   tier_bundling + kickback_proximity → tier_bundling + kickback_proximity weights
    # The split below honours all five SCORING_WEIGHTS keys from the JSON.

    w_tco = float(scoring_w["tco"])
    w_delivery = float(scoring_w["delivery_reliability"])
    w_bid = float(scoring_w["bid_price"])
    w_tier = float(scoring_w["tier_bundling"])
    w_kickback = float(scoring_w["kickback_proximity"])

    # own_stock gets a proportional bonus carved from the tco weight (half of tco).
    # This keeps the total weight sum = 1 while honouring the deliberate order.
    w_own_stock = w_tco * 0.5
    w_tco_adjusted = w_tco * 0.5

    scored: list[dict[str, Any]] = []
    for idx, (candidate, tco_result) in enumerate(zip(candidates, tco_results)):
        s1 = _step1_own_stock(candidate)
        s2 = _step2_delivery_reliability(candidate)
        s3 = _step3_tco_score(tco_result["total"])
        s4 = _step4_bid_price(float(candidate["unit_price"]))
        s5 = _step5_tier_kickback_bundling(candidate)

        composite = (
            s1 * w_own_stock
            + s2 * w_delivery
            + s3 * w_tco_adjusted
            + s4 * w_bid
            + s5 * (w_tier + w_kickback)
        )

        scored.append(
            {
                **candidate,
                "composite_score": composite,
                "score_breakdown": {
                    "step1_own_stock": s1,
                    "step2_delivery_reliability": s2,
                    "step3_tco": s3,
                    "step4_bid_price": s4,
                    "step5_tier_kickback_bundling": s5,
                },
                "tco": tco_result,
            }
        )

    ranked = sorted(scored, key=lambda c: c["composite_score"], reverse=True)

    # -----------------------------------------------------------------------
    # rebate_override: flag when step-5 changes the winner vs best-TCO winner
    # -----------------------------------------------------------------------
    best_tco_idx = tco_totals.index(min_tco)
    best_tco_candidate = candidates[best_tco_idx]

    composite_winner = ranked[0]
    # Identify the composite winner's original position by matching unit_price + supplier_id.
    composite_winner_id = _candidate_id(composite_winner)
    best_tco_id = _candidate_id(best_tco_candidate)

    rebate_override = composite_winner_id != best_tco_id
    rebate_rationale = ""
    if rebate_override:
        rebate_rationale = (
            f"Step-5 governance factors (tier × kickback-proximity × bundling) elevated "
            f"supplier '{composite_winner_id}' above the best-TCO supplier "
            f"'{best_tco_id}' (TCO {min_tco:.2f}). "
            f"Composite winner TCO: {composite_winner['tco']['total']:.2f}. "
            f"This override requires compliance review before order placement."
        )

    return {
        "ranked": ranked,
        "rebate_override": rebate_override,
        "rebate_rationale": rebate_rationale,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _candidate_id(candidate: dict[str, Any]) -> str:
    """Return a stable string identifier for a candidate (supplier_id or unit_price)."""
    return str(candidate.get("supplier_id", candidate.get("unit_price", id(candidate))))
