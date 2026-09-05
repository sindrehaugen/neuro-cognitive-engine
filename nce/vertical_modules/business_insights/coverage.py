"""
nce/vertical_modules/business_insights/coverage.py
==================================================
Confidence and Coverage metrics for Module 16 (Business Insights Engine).

Enforces BI-2:
Every finding carries: "based on N engines, M fully reconciled, K with structured attribution."
A finding leaning on a stale or attribution-poor slice is flagged, not asserted.
"""

from __future__ import annotations

from typing import Any


def compute_coverage_indicator(
    engines_evaluated: list[str],
    engine_details: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Compute structured coverage indicator across evaluated engines.

    Each engine detail can indicate:
      - 'reconciled': bool (e.g. Economy reconciled books)
      - 'structured_attribution': bool (e.g. Project/Support causal outcome attribution)
      - 'live': bool (whether the engine is live or grace-degraded)
    """
    if engine_details is None:
        engine_details = {}

    total_n = len(engines_evaluated)
    reconciled_m = 0
    structured_k = 0
    flags: list[str] = []

    for eng in engines_evaluated:
        info = engine_details.get(eng, {})
        is_live = info.get("live", True)
        if not is_live:
            flags.append(f"{eng}: engine not landed / slice grace-degraded")
            continue

        is_reconciled = info.get("reconciled", True)
        has_attribution = info.get("structured_attribution", True)

        if is_reconciled:
            reconciled_m += 1
        else:
            flags.append(f"{eng}: books not reconciled or divergence detected")

        if has_attribution:
            structured_k += 1
        else:
            flags.append(f"{eng}: lacking structured outcome attribution")

    is_low_coverage = (reconciled_m < total_n) or (structured_k < total_n) or (len(flags) > 0)
    summary = f"based on {total_n} engines, {reconciled_m} fully reconciled, {structured_k} with structured attribution"

    return {
        "total_engines": total_n,
        "reconciled_engines": reconciled_m,
        "structured_attribution_engines": structured_k,
        "summary": summary,
        "is_low_coverage": is_low_coverage,
        "flagged": is_low_coverage,
        "flags": flags,
    }
