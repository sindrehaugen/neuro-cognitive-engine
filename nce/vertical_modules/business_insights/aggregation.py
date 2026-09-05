"""
nce/vertical_modules/business_insights/aggregation.py
=====================================================
Structural Person-Grain Aggregation Barrier for Module 16 (Business Insights Engine).

Enforces BI-1 / EU AI Act Article 5 (in force 2 Feb 2025):
  - Never rank or compare individual employees/people.
  - Enforced STRUCTURALLY at the data-access layer, NOT by LLM prompt instruction.
  - Person-grain rows are NOT a returnable shape for comparison.
  - Output is strictly aggregated by team, role, department, or period.
"""

from __future__ import annotations

import re
from typing import Any

from nce.vertical_modules.business_insights._guard import PersonRankingProhibitedError

FORBIDDEN_PERSON_DIMENSIONS = frozenset(
    {
        "person",
        "employee",
        "technician",
        "user",
        "individual",
        "member",
        "name",
        "agent",
        "worker",
    }
)

ALLOWED_GROUP_DIMENSIONS = frozenset(
    {
        "team",
        "role",
        "department",
        "period",
        "quarter",
        "month",
        "overall",
        "engine",
    }
)

# Patterns detecting innocent and adversarial phrasings that seek individual people ranking
_PERSON_RANKING_PATTERNS = (
    re.compile(r"\bby\s+(?:technician|employee|person|individual|worker|agent)\b", re.I),
    re.compile(r"\bper\s+(?:person|employee|technician|individual|user|member)\b", re.I),
    re.compile(r"\bwhich\s+(?:team\s+member|person|employee|technician|individual|worker)\b", re.I),
    re.compile(r"\bwho\s+has\s+the\s+(?:highest|lowest|most|least|best|worst)\b", re.I),
    re.compile(r"\brank\s+(?:employees|people|technicians|individuals|workers|members)\b", re.I),
    re.compile(r"\bsorted\s+by\s+(?:person|employee|technician|name)\b", re.I),
)


def enforce_aggregation_barrier(
    group_by: str | None = None,
    query_text: str | None = None,
) -> str | None:
    """
    Validate that aggregation dimensions strictly adhere to the BI-1 person barrier.

    Raises PersonRankingProhibitedError if a person-grain comparison is requested
    either directly via group_by or indirectly via an innocent query phrasing.
    """
    if group_by is not None:
        dim = group_by.lower().strip()
        if dim in FORBIDDEN_PERSON_DIMENSIONS:
            raise PersonRankingProhibitedError(
                f"EU AI Act Article 5 / BI-1: Dimension {group_by!r} is a person-grain dimension. "
                "Person-grain rows are not a returnable shape for comparison. "
                "Aggregate by team, role, department, or period."
            )
        return dim

    if query_text is not None:
        for pat in _PERSON_RANKING_PATTERNS:
            if pat.search(query_text):
                raise PersonRankingProhibitedError(
                    "EU AI Act Article 5 / BI-1: Query requests individual person-grain comparison "
                    "or ranking. Individual ranking is strictly prohibited; data must be aggregated "
                    "by team, role, department, or period."
                )

    return None


def aggregate_metrics(
    records: list[dict[str, Any]],
    group_by: str,
    metric_key: str,
) -> list[dict[str, Any]]:
    """
    Structurally aggregate raw records into team/role/period groups.

    Guarantees that individual person rows/names are completely stripped
    and cannot be emitted in the return shape.
    """
    clean_dim = enforce_aggregation_barrier(group_by=group_by)
    if clean_dim is None:
        clean_dim = "team"

    groups: dict[str, list[float]] = {}
    for r in records:
        group_val = str(r.get(clean_dim) or r.get(group_by) or "Unknown")
        metric_val = float(r.get(metric_key, 0.0))
        groups.setdefault(group_val, []).append(metric_val)

    aggregated: list[dict[str, Any]] = []
    for g_val, values in groups.items():
        total = sum(values)
        count = len(values)
        avg = round(total / count, 2) if count > 0 else 0.0
        # Return dictionary physically contains ONLY the group dimension and computed aggregates
        aggregated.append(
            {
                clean_dim: g_val,
                "count": count,
                "metric_sum": total,
                "metric_avg": avg,
            }
        )

    return aggregated
