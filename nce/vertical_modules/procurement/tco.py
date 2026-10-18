"""
nce/vertical_modules/procurement/tco.py
=========================================
Pure TCO (Total Cost of Ownership) calculation — zero DB, zero HTTP, zero web/admin imports.

Reconstructed near-1:1 from the reference implementation.

TCO formula
-----------
All component weights are read from the ``weights`` dict arg (from
``procurement-weights.json``).  No literal weight constants live in this file.

    price          = supplier["unit_price"] × bom_line["quantity"]
    freight        = price × weights["freight"]
    warranty       = (bom_line["unit_price"] × bom_line["quantity"]) × weights["warranty"]
                     ^^^^  closes the warrantyCost=0 gap (round-2 #4): warranty is a
                           fraction of the *bom_line* value, not hardcoded 0.
    stock          = price × weights["stock"]
    delivery_risk  = price × weights["delivery_risk"]
    total          = price + freight + warranty + stock + delivery_risk

``bom_line["unit_price"]`` is the buyer's reference cost (what we expect to pay per unit).
``supplier["unit_price"]`` is the quoted price from this particular supplier.
When ``bom_line["unit_price"]`` is absent it defaults to ``supplier["unit_price"]``
so that warranty is always non-zero for a non-zero line — this is the safe, auditable
default; callers that supply explicit bom_line unit prices get precise warranty attribution.

Loader helper
-------------
``load_procurement_config()`` reads both JSON files from ``nce/config_data/`` and returns
``(weights_dict, tolerances_dict)``.  It is intentionally a thin file-I/O helper with no
config class — mirrors the ``product-relation-weights.json`` load pattern used in Module 2.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config loader — reads from nce/config_data/ (no config class)
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"


def load_procurement_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load and return (weights, tolerances) from the two procurement JSON files.

    Returns
    -------
    weights:
        Contents of ``procurement-weights.json`` (TCO_WEIGHTS + SCORING_WEIGHTS).
    tolerances:
        Contents of ``procurement-tolerances.json`` (MATCH_TOLERANCE + DEFAULT_THRESHOLDS).
    """
    weights_path = _CONFIG_DATA_DIR / "procurement-weights.json"
    tolerances_path = _CONFIG_DATA_DIR / "procurement-tolerances.json"

    with weights_path.open(encoding="utf-8") as fh:
        weights: dict[str, Any] = json.load(fh)

    with tolerances_path.open(encoding="utf-8") as fh:
        tolerances: dict[str, Any] = json.load(fh)

    return weights, tolerances


# ---------------------------------------------------------------------------
# Pure domain core
# ---------------------------------------------------------------------------


def do_calculate_tco(
    weights: dict[str, Any],
    tolerances: dict[str, Any],
    supplier: dict[str, Any],
    bom_line: dict[str, Any],
) -> dict[str, Any]:
    """Return a TCO breakdown for one (supplier, bom_line) pair.

    All multipliers are read from ``weights["TCO_WEIGHTS"]`` — no literal weight
    constants are present in this function.

    Parameters
    ----------
    weights:
        Loaded from ``procurement-weights.json``; must contain ``TCO_WEIGHTS`` with
        keys ``freight``, ``warranty``, ``stock``, ``delivery_risk``.
    tolerances:
        Loaded from ``procurement-tolerances.json``; unused by TCO itself but kept
        as a parameter so callers share a single config-load call with match functions.
    supplier:
        Must contain ``unit_price`` (float) — the quoted price per unit.
        Optional: ``quantity`` (int, default 1).
    bom_line:
        Must contain ``quantity`` (int) — units being procured.
        Optional: ``unit_price`` (float) — buyer's reference cost per unit.
        Defaults to ``supplier["unit_price"]`` when absent (safe/auditable default).

    Returns
    -------
    dict with keys:
        ``price``         — total quoted cost (supplier unit_price × quantity).
        ``freight``       — freight cost component.
        ``warranty``      — warranty cost component (closes warrantyCost=0 gap).
        ``stock``         — inventory/stock-holding cost component.
        ``delivery_risk`` — risk premium for late/failed delivery.
        ``total``         — sum of all five components.

    Raises
    ------
    ValueError:
        When required keys are missing or values are negative.
    KeyError:
        When ``weights`` does not contain ``TCO_WEIGHTS`` with the required sub-keys.
    """
    tco_w: dict[str, Any] = weights["TCO_WEIGHTS"]

    # --- Extract and validate inputs ---
    supplier_unit_price = _require_positive_float(supplier, "unit_price", "supplier")
    quantity = _require_positive_int(bom_line, "quantity", "bom_line")

    # Buyer's reference cost per unit — defaults to supplier's price when absent.
    # Warranty is computed on bom_line value (what we budgeted/expect to pay),
    # not the supplier's quote — important when BID prices are used.
    bom_unit_price = float(bom_line.get("unit_price", supplier_unit_price))
    if bom_unit_price < 0:
        raise ValueError("bom_line['unit_price'] must not be negative")

    # --- TCO components (all multipliers come from config, never hardcoded) ---
    price = supplier_unit_price * quantity

    # Freight: proportion of total quoted cost.
    freight = price * float(tco_w["freight"])

    # Warranty: fraction of the *bom_line* value (closes warrantyCost=0 gap).
    # Formula: bom_line_value × warranty_weight
    #   where bom_line_value = bom_unit_price × quantity
    bom_line_value = bom_unit_price * quantity
    warranty = bom_line_value * float(tco_w["warranty"])

    # Stock: holding cost as a fraction of quoted price.
    stock = price * float(tco_w["stock"])

    # Delivery risk: risk premium as a fraction of quoted price.
    delivery_risk = price * float(tco_w["delivery_risk"])

    total = price + freight + warranty + stock + delivery_risk

    result: dict[str, Any] = {
        "price": price,
        "freight": freight,
        "warranty": warranty,
        "stock": stock,
        "delivery_risk": delivery_risk,
        "total": total,
    }

    surcharge = _extract_metal_surcharge(bom_line, supplier, quantity)
    if surcharge is not None:
        total += surcharge
        result["metal_surcharge"] = round(surcharge, 2)
        result["total"] = total

    return result


# ---------------------------------------------------------------------------
# Private helpers — single level of abstraction
# ---------------------------------------------------------------------------


def _extract_metal_surcharge(
    bom_line: dict[str, Any],
    supplier: dict[str, Any],
    quantity: int,
) -> float | None:
    """Calculate Norwegian wholesaler cable metal surcharge (Kobbertillegg) if configured.

    Returns the surcharge in NOK rounded to 2 decimal places, or None if no metal
    surcharge parameters are present in bom_line or supplier.
    """
    # 1. Direct or precomputed metal surcharge per unit
    surcharge_per_unit = supplier.get("metal_surcharge_per_unit")
    if surcharge_per_unit is None:
        surcharge_per_unit = bom_line.get("metal_surcharge_per_unit")

    if surcharge_per_unit is not None:
        unit_val = float(surcharge_per_unit)
        if unit_val < 0:
            raise ValueError("metal_surcharge_per_unit must not be negative")
        length_m = float(bom_line.get("cable_length_m", quantity))
        return round(unit_val * length_m, 2)

    # Direct total metal surcharge
    direct_surcharge = supplier.get("metal_surcharge")
    if direct_surcharge is None:
        direct_surcharge = bom_line.get("metal_surcharge")

    if direct_surcharge is not None:
        val = float(direct_surcharge)
        if val < 0:
            raise ValueError("metal_surcharge must not be negative")
        return round(val, 2)

    # 2. Formula-based Norwegian wholesaler Kobbertillegg / Aluminiumstillegg
    metal_weight = bom_line.get("metal_weight_kg_per_km")
    if metal_weight is None:
        metal_weight = bom_line.get("copper_weight_kg_per_km")
    if metal_weight is None:
        metal_weight = supplier.get("metal_weight_kg_per_km")
    if metal_weight is None:
        metal_weight = supplier.get("copper_weight_kg_per_km")

    lme_rate = supplier.get("lme_rate_nok_per_kg")
    if lme_rate is None:
        lme_rate = bom_line.get("lme_rate_nok_per_kg")

    if metal_weight is not None and lme_rate is not None:
        from nce.vertical_modules.product.enrich import calculate_cable_metal_surcharge

        baseline_rate = supplier.get("baseline_rate_nok_per_kg")
        if baseline_rate is None:
            baseline_rate = bom_line.get("baseline_rate_nok_per_kg", 0.0)

        length_m = float(bom_line.get("cable_length_m", quantity))
        surcharge_dict = calculate_cable_metal_surcharge(
            length_m=length_m,
            metal_weight_kg_per_km=float(metal_weight),
            lme_rate_nok_per_kg=float(lme_rate),
            baseline_rate_nok_per_kg=float(baseline_rate),
        )
        return round(float(surcharge_dict["surcharge_amount_nok"]), 2)

    return None


def _require_positive_float(d: dict[str, Any], key: str, context: str) -> float:
    """Return d[key] as a positive float or raise ValueError."""
    if key not in d:
        raise ValueError(f"'{key}' is required in {context}")
    value = float(d[key])
    if value < 0:
        raise ValueError(f"{context}['{key}'] must not be negative, got {value}")
    return value


def _require_positive_int(d: dict[str, Any], key: str, context: str) -> int:
    """Return d[key] as a positive int or raise ValueError."""
    if key not in d:
        raise ValueError(f"'{key}' is required in {context}")
    value = int(d[key])
    if value <= 0:
        raise ValueError(f"{context}['{key}'] must be a positive integer, got {value}")
    return value
