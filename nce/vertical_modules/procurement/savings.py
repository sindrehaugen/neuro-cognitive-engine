"""
nce/vertical_modules/procurement/savings.py
============================================
Savings aggregation and leakage detection — Module 1 Wave 9.

Reconstructed from Andreas's ``lib/procurement/savings-aggregator.ts``
(reference IP, not in repo).

Architecture
------------
Pure aggregation core (no DB import):
  ``aggregate_savings(spend_rows, baselines) -> dict``
    Computes realised / lost savings and leakage candidates over plain
    dicts.  Zero DB, zero HTTP.  Unit-testable without any infrastructure.

DB wrapper:
  ``do_aggregate_savings(engine, params) -> dict``
    Gathers the period's spend rows from ``procurement_spend_lines``
    (namespace-scoped via ``scoped_pg_session``) and feeds the pure core.
    Read-only Watcher — no graph writes, no INSERT/UPDATE.

Formulas
--------
For each spend row ``r``:
  - ``baseline_price`` = ``baselines[r["artnr"]]["best_bid"]`` if available,
    else ``r["unit_price"]`` (no savings known — neutral).
  - ``baseline_total`` = ``baseline_price × r["quantity"]``.
  - ``actual_total``   = ``r["unit_price"]  × r["quantity"]``.

  realised savings (basket):
    Sum of ``(baseline_total − actual_total)`` where ``actual_total <=
    baseline_total`` (we paid the same or less than baseline → money saved).

  lost savings (basket):
    Sum of ``(actual_total − baseline_total)`` where ``actual_total >
    baseline_total`` and a baseline exists (a cheaper option was available
    but not taken → overspend relative to baseline).

  leakage candidate:
    A spend row where ``r["unit_price"] > baselines[r["artnr"]]["best_bid"]``
    (actual unit price exceeded the best available BID/contract price).
    Flagged with:
      ``artnr``        — article identifier
      ``actual_price`` — the unit price paid
      ``best_bid``     — the best available BID price
      ``gap``          — actual_price − best_bid (positive)
      ``quantity``     — units purchased
      ``gap_total``    — gap × quantity
      ``rationale``    — human-readable explanation

Result shape
------------
``{
    "realised": float,          # total money saved vs baseline (>= 0)
    "lost":     float,          # total overspend vs baseline (>= 0)
    "leakage_candidates": [     # list of dicts (possibly empty)
        {
            "artnr":        str,
            "actual_price": float,
            "best_bid":     float,
            "gap":          float,
            "quantity":     int | float,
            "gap_total":    float,
            "rationale":    str,
        },
        ...
    ],
}``

Watcher invariants
------------------
- ``do_aggregate_savings`` performs only SELECT queries.
- No INSERT, UPDATE, DELETE, or graph write anywhere in this module.
- All reads run inside ``scoped_pg_session`` (RLS enforced per tenant).
- ``require_namespace_id`` validates the caller supplied a namespace.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.procurement.savings")


# ---------------------------------------------------------------------------
# Pure aggregation core — no DB, no HTTP, no web imports
# ---------------------------------------------------------------------------


def aggregate_savings(
    spend_rows: list[dict[str, Any]],
    baselines: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compute realised/lost savings and leakage candidates over plain dicts.

    Parameters
    ----------
    spend_rows:
        List of spend-line dicts.  Each must contain:
          ``artnr``      (str)         — article identifier.
          ``unit_price`` (float)       — price actually paid per unit.
          ``quantity``   (int | float) — units purchased.
        Optional keys are ignored.

    baselines:
        Mapping from artnr → baseline dict.  The baseline dict must contain:
          ``best_bid`` (float) — best available BID/contract price per unit.
        When an artnr has no entry in ``baselines`` it is treated as
        neutral (baseline = actual → no savings, no loss, no leakage).

    Returns
    -------
    dict with keys:
      ``realised``            — float, total money saved vs baseline (>= 0).
      ``lost``                — float, total overspend vs baseline (>= 0).
      ``leakage_candidates``  — list of leakage candidate dicts (may be empty).

    Raises
    ------
    ValueError
        When a spend row is missing ``artnr``, ``unit_price``, or ``quantity``.
    """
    realised: float = 0.0
    lost: float = 0.0
    leakage_candidates: list[dict[str, Any]] = []

    for row in spend_rows:
        artnr = row.get("artnr")
        if artnr is None:
            raise ValueError(f"spend row missing 'artnr': {row!r}")

        raw_price = row.get("unit_price")
        if raw_price is None:
            raise ValueError(f"spend row missing 'unit_price': {row!r}")
        actual_price = float(raw_price)

        raw_qty = row.get("quantity")
        if raw_qty is None:
            raise ValueError(f"spend row missing 'quantity': {row!r}")
        quantity = float(raw_qty)

        baseline_entry = baselines.get(str(artnr))

        if baseline_entry is None:
            # No baseline available — neutral row; contributes nothing to realised/lost.
            continue

        best_bid = float(baseline_entry["best_bid"])

        baseline_total = best_bid * quantity
        actual_total = actual_price * quantity
        delta = baseline_total - actual_total  # positive → saved; negative → overspent

        if delta > 0:
            realised += delta
        elif delta < 0:
            lost += abs(delta)

        # Leakage: actual unit price exceeded best available BID price.
        if actual_price > best_bid:
            gap = actual_price - best_bid
            gap_total = gap * quantity
            leakage_candidates.append(
                {
                    "artnr": str(artnr),
                    "actual_price": actual_price,
                    "best_bid": best_bid,
                    "gap": gap,
                    "quantity": quantity,
                    "gap_total": gap_total,
                    "rationale": (
                        f"Unit price {actual_price:.4f} exceeded best available BID "
                        f"{best_bid:.4f} for artnr '{artnr}' "
                        f"(gap {gap:.4f} × {quantity} units = {gap_total:.4f} total leakage)."
                    ),
                }
            )

    return {
        "realised": realised,
        "lost": lost,
        "leakage_candidates": leakage_candidates,
    }


# ---------------------------------------------------------------------------
# DB read helpers — keep narrow and private
# ---------------------------------------------------------------------------


async def _fetch_spend_rows(
    conn: Any,
    namespace_id: uuid.UUID,
    period_start: str,
    period_end: str,
) -> list[dict[str, Any]]:
    """SELECT spend lines for the given period.  Read-only (Watcher)."""
    # procurement_spend_lines is wired by a later wave / Economy A2A feed. Until
    # it exists, the read-only savings Watcher reports no spend rather than
    # crashing (graceful degradation, same posture as the EOL Watcher). Detected
    # via to_regclass so this module keeps NO DB-driver import (pure-core guard).
    table_exists = await conn.fetchval("SELECT to_regclass('public.procurement_spend_lines')")
    if table_exists is None:
        log.info("[procurement-savings] procurement_spend_lines absent — no spend to aggregate yet")
        return []

    rows = await conn.fetch(
        """
        SELECT artnr, unit_price, quantity
        FROM   procurement_spend_lines
        WHERE  namespace_id = $1
          AND  spend_date  >= $2::date
          AND  spend_date  <  $3::date
        ORDER  BY artnr
        """,
        namespace_id,
        period_start,
        period_end,
    )
    return [dict(r) for r in rows]


async def _fetch_best_bids_for_artnrs(
    conn: Any,
    namespace_id: uuid.UUID,
    artnrs: list[str],
) -> dict[str, dict[str, Any]]:
    """Return {artnr: {best_bid: float}} from procurement_bid_prices.  Read-only."""
    if not artnrs:
        return {}

    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (artnr) artnr, pris AS best_bid
        FROM   procurement_bid_prices
        WHERE  namespace_id = $1
          AND  artnr        = ANY($2::text[])
          AND  pris         IS NOT NULL
        ORDER  BY artnr, pris ASC
        """,
        namespace_id,
        artnrs,
    )
    return {r["artnr"]: {"best_bid": float(r["best_bid"])} for r in rows}


# ---------------------------------------------------------------------------
# Public Watcher wrapper
# ---------------------------------------------------------------------------


async def do_aggregate_savings(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate realised/lost savings and leakage candidates for a period.

    Parameters
    ----------
    engine:
        The live NCEEngine instance (provides ``pg_pool``).
    params:
        Required keys:
          ``namespace_id`` (str, UUID) — tenant namespace.
          ``period_start`` (str)       — inclusive start date (ISO 8601, ``YYYY-MM-DD``).
          ``period_end``   (str)       — exclusive end date   (ISO 8601, ``YYYY-MM-DD``).

    Returns
    -------
    dict
        ``{
            "realised": float,
            "lost":     float,
            "leakage_candidates": [{artnr, actual_price, best_bid,
                                    gap, quantity, gap_total, rationale}, ...]
        }``

    Raises
    ------
    ValueError
        When ``namespace_id``, ``period_start``, or ``period_end`` are missing or invalid.

    Notes
    -----
    Watcher role — strictly read-only.  No INSERT, UPDATE, or graph write is
    performed at any point in this call chain.
    """
    raw_ns = require_namespace_id(params)
    namespace_id = uuid.UUID(raw_ns)

    period_start: str | None = params.get("period_start")
    period_end: str | None = params.get("period_end")
    if not period_start:
        raise ValueError("do_aggregate_savings: 'period_start' is required")
    if not period_end:
        raise ValueError("do_aggregate_savings: 'period_end' is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        spend_rows = await _fetch_spend_rows(conn, namespace_id, period_start, period_end)

        artnrs = list({r["artnr"] for r in spend_rows if r.get("artnr")})
        baselines = await _fetch_best_bids_for_artnrs(conn, namespace_id, artnrs)

    result = aggregate_savings(spend_rows, baselines)

    log.info(
        "[procurement-savings] namespace=%s period=%s..%s "
        "spend_rows=%d realised=%.2f lost=%.2f leakage=%d",
        namespace_id,
        period_start,
        period_end,
        len(spend_rows),
        result["realised"],
        result["lost"],
        len(result["leakage_candidates"]),
    )

    return result
