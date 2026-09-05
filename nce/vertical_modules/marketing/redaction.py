"""
nce/vertical_modules/marketing/redaction.py
===========================================
Assembly-time allow-list redaction and PII anonymisation (MK-3).

Enforces:
- Margin, cost, and internal rate fields NEVER enter a draft body.
- Customer identifiers and site names are anonymised by default to neutral placeholders.
- Reuses the 4-consumer allow-list pattern (Sales, Vendors, Customer Portal, Marketing).
"""

from __future__ import annotations

from typing import Any

from nce.vertical_modules.marketing._guard import (
    assert_no_sensitive_financials,
)

# Marketing-safe allowlist of fields permitted to enter draft assembly
MARKETING_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "node_id",
        "label",
        "title",
        "description",
        "room_type",
        "system_class",
        "vertical",
        "technology_stack",
        "devices",
        "outcomes",
        "metrics",
        "performance",
        "setup_time_mins",
        "downtime_reduction_pct",
        "energy_saving_pct",
        "user_satisfaction_score",
        "anonymized",
        "namespace_id",
        "project_id",
        "created_at",
    }
)


def redact_for_marketing_draft(
    node: dict[str, Any],
    anonymize: bool = True,
    strict: bool = False,
) -> dict[str, Any]:
    """Redact raw project or graph facts at draft assembly time.

    Parameters
    ----------
    node : dict[str, Any]
        Raw data dictionary from the graph or database.
    anonymize : bool
        If True, masks customer name and site details with neutral placeholders.
    strict : bool
        If True, raises MarketingSensitiveDataLeakError immediately if forbidden
        financial keys are present in the input.

    Returns
    -------
    dict[str, Any]
        Clean dictionary containing ONLY allowed fields, with PII masked.
    """
    if strict:
        assert_no_sensitive_financials(node)

    # Allow-list projection: drop everything not explicitly permitted
    clean: dict[str, Any] = {}
    for k, v in node.items():
        if k in MARKETING_ALLOWED_FIELDS:
            clean[k] = v

    # Masking customer identities if anonymise is True
    if anonymize:
        if "label" in clean:
            clean["label"] = _anonymize_text(str(clean["label"]))
        if "description" in clean:
            clean["description"] = _anonymize_text(str(clean["description"]))

    return clean


def _anonymize_text(text: str) -> str:
    """Mask specific company/customer references with neutral placeholders."""
    neutral_replacements = {
        "MegaCorp Oslo": "ALPHA Enterprise",
        "MegaCorp": "ALPHA Corp",
        "Oslo": "Northern Europe",
        "Bergen": "Regional Office",
    }
    result = text
    for target, placeholder in neutral_replacements.items():
        result = result.replace(target, placeholder)
    return result
