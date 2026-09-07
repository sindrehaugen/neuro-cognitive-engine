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
        "engineer",
        "consultant",
        "staffer",
        "operator",
        "installer",
        "rep",
        "tech",
        "contractor",
        "ansatt",
        "montør",
        "medarbeider",
        "konsulent",
        "utvikler",
        "sjåfør",
        "arbeider",
        "person_id",
        "employee_id",
        "technician_id",
        "user_id",
        "worker_id",
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
    # English patterns
    re.compile(
        r"\b(?:by|per|across|for)\s+(?:each\s+)?(?:technician|employee|person|individual|worker|agent|user|member|engineer|consultant|staffer|operator|installer|rep|tech|contractor)s?\b",
        re.I,
    ),
    re.compile(
        r"\bwhich\s+(?:team\s+member|person|employee|technician|individual|worker|engineer|consultant|staffer|operator|installer|rep|tech|contractor)\b",
        re.I,
    ),
    re.compile(
        r"\bwho\s+(?:has\s+the|is\s+the|had\s+the|closed|resolved|sold|billed|completed|logged)\s+(?:highest|lowest|most|least|fewest|best|worst|top|bottom)\b",
        re.I,
    ),
    re.compile(
        r"\bwho\s+(?:closed|resolved|sold|billed|completed|logged)\s+(?:the\s+)?(?:most|least|fewest)\b",
        re.I,
    ),
    re.compile(
        r"\brank\s+(?:employees|people|technicians|individuals|workers|members|engineers|consultants|staffers|operators|installers|reps|techs|contractors)\b",
        re.I,
    ),
    re.compile(
        r"\bsorted\s+by\s+(?:person|employee|technician|individual|worker|engineer|consultant|name)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:list|show|get|rank)\s+(?:all\s+)?(?:technicians|employees|people|individuals|workers|engineers|consultants|staffers|operators|installers|reps|techs|contractors|ansatte|montører|medarbeidere|teknikere)\s+sorted\s+by\b",
        re.I,
    ),
    re.compile(
        r"\b(?:top|bottom|best|worst|highest|lowest)\s+(?:performing\s+)?(?:employees|people|technicians|individuals|workers|members|engineers|consultants|staffers|operators|installers|reps|techs)\b",
        re.I,
    ),
    re.compile(
        r"\bcompare\s+(?:individual\s+)?(?:employees|people|technicians|individuals|workers|engineers|consultants)\b",
        re.I,
    ),
    # Norwegian patterns (strict \b word boundary to avoid matching belysning or personopplysninger)
    re.compile(
        r"\b(?:etter|pr|per|for\s+hver)\s+(?:ansatt|montør|medarbeider|konsulent|utvikler|tekniker|sjåfør|arbeider)e?r?\b",
        re.I,
    ),
    re.compile(
        r"\bhvilken\s+(?:ansatt|montør|medarbeider|konsulent|utvikler|tekniker|sjåfør|arbeider)\b",
        re.I,
    ),
    re.compile(
        r"\bhvem\s+(?:har\s+(?:den\s+)?|er\s+(?:den\s+)?|solgte|lukket|fakturerte|utførte|leverte)\s*(?:høyest|lavest|mest|minst|best|dårligst|færrest)\b",
        re.I,
    ),
    re.compile(
        r"\bhvem\s+(?:solgte|lukket|fakturerte|utførte|leverte)\s+(?:mest|minst|best|dårligst|færrest)\b",
        re.I,
    ),
    re.compile(
        r"\branger\s+(?:ansatte|montører|medarbeidere|konsulenter|utviklere|teknikere|arbeidere)\b",
        re.I,
    ),
    re.compile(
        r"\bsortert\s+etter\s+(?:ansatt|montør|medarbeider|navn|tekniker|person)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:best|dårligst|mest|minst)\s+(?:ytende\s+)?(?:ansatte|montører|medarbeidere|konsulenter|utviklere|teknikere)\b",
        re.I,
    ),
    re.compile(
        r"\bsammenlign\s+(?:individuelle\s+)?(?:ansatte|montører|medarbeidere|teknikere)\b",
        re.I,
    ),
)


def enforce_aggregation_barrier(
    group_by: str | None = None,
    query_text: str | None = None,
) -> str | None:
    """
    Validate that aggregation dimensions strictly adhere to the BI-1 person barrier.

    The ALLOWED_GROUP_DIMENSIONS allowlist strictly decides which dimensions are permitted.
    Raises PersonRankingProhibitedError if:
    1. A query_text phrasing requests individual person-grain comparison or ranking.
       Checked FIRST and regardless of group_by -- callers pass both.
    2. A group_by dimension is not in ALLOWED_GROUP_DIMENSIONS.
    """
    # 🔴 query_text is checked FIRST and UNCONDITIONALLY. It used to be checked
    # only when group_by was None, and ask.py:86 passes BOTH -- so any allowed
    # group_by ("team") returned early and the person-ranking phrasings were
    # never inspected. A compliance guard that a caller can skip by supplying a
    # second, valid argument is not a guard.
    if query_text is not None:
        for pat in _PERSON_RANKING_PATTERNS:
            if pat.search(query_text):
                raise PersonRankingProhibitedError(
                    "EU AI Act Article 5 / BI-1: Query requests individual person-grain comparison "
                    "or ranking. Individual ranking is strictly prohibited; data must be aggregated "
                    f"by an allowed group dimension ({', '.join(sorted(ALLOWED_GROUP_DIMENSIONS))})."
                )

    if group_by is not None:
        dim = group_by.lower().strip()
        # Friendly message for obvious person-grain dimensions
        if dim in FORBIDDEN_PERSON_DIMENSIONS:
            raise PersonRankingProhibitedError(
                f"EU AI Act Article 5 / BI-1: Dimension {group_by!r} is a person-grain dimension. "
                "Person-grain rows are not a returnable shape for comparison. "
                f"Allowed dimensions: {', '.join(sorted(ALLOWED_GROUP_DIMENSIONS))}."
            )
        # Structural allowlist enforcement: ONLY ALLOWED_GROUP_DIMENSIONS pass
        if dim not in ALLOWED_GROUP_DIMENSIONS:
            raise PersonRankingProhibitedError(
                f"EU AI Act Article 5 / BI-1: Dimension {group_by!r} is not an allowed group dimension. "
                "Person-grain and non-group dimensions are strictly prohibited under EU AI Act Art. 5. "
                f"Allowed dimensions: {', '.join(sorted(ALLOWED_GROUP_DIMENSIONS))}."
            )
        return dim

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


def enforce_shape_guarantee(
    result: dict[str, Any],
    *,
    raw_records: list[dict[str, Any]] | None = None,
    group_by: str = "team",
    metric_key: str = "value",
) -> dict[str, Any]:
    """
    Structural return-shape guarantee for BI-1 (EU AI Act Article 5).

    Guarantees that:
    1. If raw records are provided, they are collapsed via aggregate_metrics()
       into an allowed group dimension and returned under 'breakdown'.
    2. Any existing tabular/breakdown records in 'result' have all person-grain
       identifiers (person, name, employee_id, etc.) stripped or aggregated.
    3. Person-grain rows are structurally NOT a returnable shape.
    """
    clean_dim = group_by.lower().strip() if group_by else "team"
    if clean_dim not in ALLOWED_GROUP_DIMENSIONS:
        clean_dim = "team"

    if raw_records is not None and isinstance(raw_records, list):
        result["breakdown"] = aggregate_metrics(
            raw_records,
            group_by=clean_dim,
            metric_key=metric_key,
        )

    # Sanitize breakdown / records lists in result if present
    for list_key in ("breakdown", "records", "metrics"):
        if list_key in result and isinstance(result[list_key], list) and result[list_key]:
            has_person_grain = any(
                any(k.lower() in FORBIDDEN_PERSON_DIMENSIONS for k in item.keys())
                for item in result[list_key]
                if isinstance(item, dict)
            )
            if has_person_grain:
                sample = result[list_key][0]
                target_group = clean_dim
                for dim in ("team", "role", "department", "period", "quarter", "month", "engine"):
                    if any(dim in item for item in result[list_key] if isinstance(item, dict)):
                        target_group = dim
                        break
                target_metric = metric_key
                for k, v in sample.items():
                    if isinstance(v, (int, float)) and k.lower() not in FORBIDDEN_PERSON_DIMENSIONS:
                        target_metric = k
                        break
                result[list_key] = aggregate_metrics(
                    result[list_key],
                    group_by=target_group,
                    metric_key=target_metric,
                )

    return result
