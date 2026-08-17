"""
nce.pricing.dg — DG-based pricing core.

Pure domain core: dg_price(cost, dg_pct) = cost / (1 - dg_pct).
Cost and margin are internal domain-logic values; they NEVER cross to
a customer-facing surface (ADR-0017: cost/margin confidentiality).
Callers must ensure the returned value is redacted via C8 field-level
projection before any external API or response includes it.

Per-namespace DG% is loaded from nce/config_data/product-dg.json.
"""

import json
from pathlib import Path


def dg_price(cost: float, dg_pct: float) -> float:
    """
    Calculate sales price from cost and DG% (discount gross).

    Formula: sales_price = cost / (1 - dg_pct)

    Args:
        cost: Internal cost value (not to be exposed externally; see docstring).
        dg_pct: Discount as a percentage in [0, 1). At 0, returns cost.
                At 0.3, cost/0.7 (30% margin on sales_price).

    Returns:
        Sales price (internal domain value; must be redacted before
        customer-facing surfaces per ADR-0017).

    Raises:
        ValueError: If dg_pct < 0 or dg_pct >= 1.
        ZeroDivisionError: Never raised by guard; raised only if guard fails.

    Example:
        dg_price(100, 0.3) == 100 / 0.7 ≈ 142.857
    """
    if dg_pct < 0 or dg_pct >= 1:
        raise ValueError(
            f"dg_pct must be in [0, 1); got {dg_pct}. "
            f"At dg_pct >= 1, division by zero is undefined."
        )
    return cost / (1 - dg_pct)


def load_dg(namespace: str) -> float:
    """
    Load the per-namespace DG% from nce/config_data/product-dg.json.

    Args:
        namespace: Namespace key (e.g., "default", "partner_x").

    Returns:
        DG% as a float in [0, 1).

    Raises:
        FileNotFoundError: If product-dg.json does not exist.
        KeyError: If namespace not found in the JSON.
        ValueError: If the loaded DG% is not in [0, 1).
    """
    config_path = Path(__file__).parent.parent / "config_data" / "product-dg.json"

    if not config_path.exists():
        raise FileNotFoundError(
            f"product-dg.json not found at {config_path}. "
            f"Ensure nce/config_data/product-dg.json is seeded."
        )

    with open(config_path) as f:
        config = json.load(f)

    if namespace not in config:
        raise KeyError(
            f"Namespace '{namespace}' not found in product-dg.json. "
            f"Available: {list(config.keys())}"
        )

    dg = config[namespace]

    # Validate the loaded DG% before returning.
    if not isinstance(dg, (int, float)) or dg < 0 or dg >= 1:
        raise ValueError(f"Invalid DG% for namespace '{namespace}': {dg}. Must be in [0, 1).")

    return float(dg)
