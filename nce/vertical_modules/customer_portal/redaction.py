"""
nce/vertical_modules/customer_portal/redaction.py
=================================================
Explicit Field Allow-List Redaction Harness (Charter Layer 2).

Every customer-facing projection passes this allow-list before serialization:
  - An allow-list fails closed; a deny-list fails open.
  - Strips margin, cost, our-cost, supplier terms, internal status, and slip.
  - Sourced from customer-redaction.json.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent / "customer-redaction.json"


@lru_cache(maxsize=1)
def load_customer_redaction_rules() -> dict[str, Any]:
    """Load and cache the customer redaction rules."""
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Customer redaction rules not found at {_CONFIG_PATH}")
    with open(_CONFIG_PATH, encoding="utf-8-sig") as f:
        data = json.load(f)
    return data.get("projections", {})


def project_customer_safe(projection_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Project a raw dictionary through the explicit allow-list for projection_name.

    Fails closed: Any field NOT explicitly listed in allowed_fields is excluded.
    Guards against leaks: Asserts forbidden fields never pass through.
    """
    rules = load_customer_redaction_rules()
    projection_rule = rules.get(projection_name)

    if not projection_rule:
        # If projection rule is undefined, fail closed completely
        return {}

    allowed = set(projection_rule.get("allowed_fields", []))
    forbidden = set(projection_rule.get("forbidden_fields", []))

    projected: dict[str, Any] = {}
    for key, val in data.items():
        if key in allowed and key not in forbidden:
            # Format datetime / date / uuid to string if needed
            if hasattr(val, "isoformat"):
                projected[key] = val.isoformat()
            elif hasattr(val, "hex") and len(str(val)) == 36:
                projected[key] = str(val)
            else:
                projected[key] = val

    return projected


def project_customer_safe_list(
    projection_name: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Project a list of records through the allow-list."""
    return [project_customer_safe(projection_name, item) for item in items]
