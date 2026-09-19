"""
nce/vertical_modules/agreements/index_series.py
===============================================
Config-as-IP reference library for economic index series (KPI, CPI, ICT services)
for the Agreements vertical module (Wave B-10 / Revenue Lane).

Responsibilities
----------------
* Curated Norwegian and European index series (Config-as-IP):
  - SSB KPI (Norwegian official Consumer Price Index).
  - SSB KPI-JAE (Core inflation excluding energy & taxes).
  - SSB IKT-Tjenester (IT and telecom services producer price index).
  - SSB Lonn-IKT (Wage index for tech / systems engineering).
  - EU HICP (Harmonised Index of Consumer Prices for EU cross-border).
* Multi-period index uplift calculation:
  - Supports period-to-period lookups (year, year-month, quarter) or explicit index values.
  - Applies regulation ratios (e.g. 80% or 100% of index movement).
  - Enforces regulation floors (no negative deflation adjustments unless explicitly opted in)
    and caps.
* Read-only domain query and calculation functions:
  - ``do_get_index_series(engine, params)``
  - ``do_calculate_index_adjustment(engine, params)``

Design invariants (uncle-bob-craft)
------------------------------------
* Config-as-IP lives in this module directory, NEVER under ``nce/config_data/`` (Q-5).
* Read-only domain functions: no database mutations, pure deterministic math.
* Zero identifying host portal literals.
"""

from __future__ import annotations

import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger("nce.vertical_modules.agreements.index_series")

_INDEX_FILE = Path(__file__).resolve().parent / "index-series.json"

_SCALE_4DP = Decimal("0.0001")
_SCALE_2DP = Decimal("0.01")


def _quantise_pct(val: Decimal) -> Decimal:
    return val.quantize(_SCALE_4DP, rounding=ROUND_HALF_UP)


def _quantise_money(val: Decimal) -> Decimal:
    return val.quantize(_SCALE_2DP, rounding=ROUND_HALF_UP)


def load_index_series() -> list[dict[str, Any]]:
    """Load Config-as-IP economic index series from module JSON storage."""
    try:
        if _INDEX_FILE.exists():
            data = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
            return data.get("series", [])
        log.warning("Index series file %s not found", _INDEX_FILE)
        return []
    except Exception as exc:
        log.exception("Failed to load index series from %s: %s", _INDEX_FILE, exc)
        return []


def get_index_series(
    series_id: str | None = None,
    search: str | None = None,
    frequency: str | None = None,
) -> list[dict[str, Any]]:
    """Filter and retrieve index series definitions."""
    all_series = load_index_series()
    results: list[dict[str, Any]] = []

    for item in all_series:
        if series_id and item.get("series_id", "").upper() != series_id.strip().upper():
            continue
        if frequency and item.get("frequency", "").lower() != frequency.strip().lower():
            continue
        if search:
            needle = search.strip().lower()
            text_corpus = (
                f"{item.get('series_id', '')} {item.get('code', '')} "
                f"{item.get('name', '')} {item.get('description', '')}"
            ).lower()
            if needle not in text_corpus:
                continue
        results.append(item)

    return results


def calculate_index_uplift(
    series_id: str,
    base_period: str | None = None,
    target_period: str | None = None,
    base_index: Decimal | float | str | None = None,
    target_index: Decimal | float | str | None = None,
    regulation_ratio: Decimal | float | str | None = 1.0,
    cap_pct: Decimal | float | str | None = None,
    floor_pct: Decimal | float | str | None = 0.0,
) -> dict[str, Any]:
    """Calculate inflation uplift percentage based on index series measurements.

    Raises ValueError if series is not found or index values cannot be resolved.
    """
    series_list = get_index_series(series_id=series_id)
    if not series_list:
        raise ValueError(f"Unknown index series: {series_id!r}")
    series = series_list[0]
    measurements: dict[str, float] = series.get("measurements", {})

    # Resolve base index
    b_idx: Decimal
    if base_index is not None:
        b_idx = Decimal(str(base_index))
    elif base_period is not None:
        key = str(base_period).strip()
        if key not in measurements:
            raise ValueError(
                f"Base period {base_period!r} not found in index series {series_id}. "
                f"Available periods: {sorted(measurements.keys())}"
            )
        b_idx = Decimal(str(measurements[key]))
    else:
        raise ValueError("Either base_index or base_period must be supplied")

    if b_idx <= 0:
        raise ValueError(f"Base index must be positive, got {b_idx}")

    # Resolve target index
    t_idx: Decimal
    if target_index is not None:
        t_idx = Decimal(str(target_index))
    elif target_period is not None:
        key = str(target_period).strip()
        if key not in measurements:
            raise ValueError(
                f"Target period {target_period!r} not found in index series {series_id}. "
                f"Available periods: {sorted(measurements.keys())}"
            )
        t_idx = Decimal(str(measurements[key]))
    else:
        raise ValueError("Either target_index or target_period must be supplied")

    if t_idx <= 0:
        raise ValueError(f"Target index must be positive, got {t_idx}")

    # Raw inflation change: (target - base) / base
    raw_change = (t_idx - b_idx) / b_idx

    # Apply regulation ratio (e.g. 1.0 for 100%, 0.8 for 80%)
    ratio = Decimal(str(regulation_ratio)) if regulation_ratio is not None else Decimal("1.0")
    adjusted_uplift = raw_change * ratio

    # Apply floor (default 0.0 — contracts typically do not deflate prices unless explicitly configured)
    is_clamped_floor = False
    effective_uplift = adjusted_uplift
    if floor_pct is not None:
        f_val = Decimal(str(floor_pct))
        if effective_uplift < f_val:
            effective_uplift = f_val
            is_clamped_floor = True

    # Apply cap if provided
    is_clamped_cap = False
    if cap_pct is not None:
        c_val = Decimal(str(cap_pct))
        if effective_uplift > c_val:
            effective_uplift = c_val
            is_clamped_cap = True

    return {
        "series_id": series["series_id"],
        "series_name": series["name"],
        "series_code": series.get("code"),
        "base_period": base_period,
        "target_period": target_period,
        "base_index": float(b_idx),
        "target_index": float(t_idx),
        "raw_change_pct": float(_quantise_pct(raw_change)),
        "regulation_ratio": float(ratio),
        "adjusted_uplift_pct": float(_quantise_pct(adjusted_uplift)),
        "effective_uplift_pct": float(_quantise_pct(effective_uplift)),
        "is_clamped_cap": is_clamped_cap,
        "is_clamped_floor": is_clamped_floor,
        "cap_pct": float(cap_pct) if cap_pct is not None else None,
        "floor_pct": float(floor_pct) if floor_pct is not None else None,
    }


def do_get_index_series(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Domain query: list or search economic index series."""
    series_id = params.get("series_id")
    search = params.get("search")
    frequency = params.get("frequency")

    matches = get_index_series(series_id=series_id, search=search, frequency=frequency)
    return {
        "status": "ok",
        "total": len(matches),
        "series": matches,
    }


def do_calculate_index_adjustment(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Domain query: compute index-linked price adjustment and optional renewal amount."""
    series_id = params.get("series_id") or params.get("index_series_id")
    if not series_id:
        raise ValueError("do_calculate_index_adjustment: 'series_id' is required")

    base_period = params.get("base_period")
    target_period = params.get("target_period")
    base_index = params.get("base_index")
    target_index = params.get("target_index")
    regulation_ratio = params.get("regulation_ratio", 1.0)
    cap_pct = params.get("cap_pct")
    floor_pct = params.get("floor_pct", 0.0)

    result = calculate_index_uplift(
        series_id=str(series_id),
        base_period=str(base_period) if base_period is not None else None,
        target_period=str(target_period) if target_period is not None else None,
        base_index=base_index,
        target_index=target_index,
        regulation_ratio=regulation_ratio,
        cap_pct=cap_pct,
        floor_pct=floor_pct,
    )

    # If an amount was passed, calculate the adjusted amounts
    current_amount = params.get("current_annual_amount") or params.get("current_amount")
    if current_amount is not None:
        curr_d = Decimal(str(current_amount))
        eff_uplift_d = Decimal(str(result["effective_uplift_pct"]))
        adjustment_amount = curr_d * eff_uplift_d
        new_amount = curr_d + adjustment_amount

        result["current_amount"] = float(_quantise_money(curr_d))
        result["adjustment_amount"] = float(_quantise_money(adjustment_amount))
        result["new_amount"] = float(_quantise_money(new_amount))

    result["ok"] = True
    return result
