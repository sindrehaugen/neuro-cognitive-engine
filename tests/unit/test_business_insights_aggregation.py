"""
tests/unit/test_business_insights_aggregation.py
================================================
Adversarial unit tests for BI-1: Structural Person-Grain Barrier (EU AI Act Article 5).

Legal requirement:
  - Never rank people.
  - Enforced STRUCTURALLY at the data-access layer (aggregation.py).
  - Person-grain rows are NOT a returnable shape for comparison.
  - The board receives aggregates by team, role, department, or period.

Tested against the four canonical innocent phrasings from Charter §6:
  1. "average resolution time by technician"
  2. "which team member closed the most tickets last quarter"
  3. "show utilisation per person, sorted"
  4. "who has the highest ticket backlog"
"""

from __future__ import annotations

import pytest

from nce.vertical_modules.business_insights._guard import PersonRankingProhibitedError
from nce.vertical_modules.business_insights.aggregation import (
    aggregate_metrics,
    enforce_aggregation_barrier,
)


def test_aggregation_barrier_rejects_person_grain_grouping():
    """Direct grouping by person, employee, or technician must be structurally rejected."""
    forbidden_dimensions = ["person", "employee", "technician", "user", "individual", "member"]
    for dim in forbidden_dimensions:
        with pytest.raises(PersonRankingProhibitedError) as exc:
            enforce_aggregation_barrier(group_by=dim)
        assert "EU AI Act" in str(exc.value) or "person-grain" in str(exc.value).lower()


def test_aggregation_barrier_accepts_team_role_period_grouping():
    """Aggregating by team, role, department, or period is valid."""
    valid_dimensions = ["team", "role", "department", "period", "quarter"]
    for dim in valid_dimensions:
        normalized = enforce_aggregation_barrier(group_by=dim)
        assert normalized == dim


@pytest.mark.parametrize(
    "innocent_query",
    [
        "average resolution time by technician",
        "which team member closed the most tickets last quarter",
        "show utilisation per person, sorted",
        "who has the highest ticket backlog",
    ],
)
def test_innocent_phrasings_blocked_from_person_grain_return(innocent_query: str):
    """
    BI-1 Adversarial Test:
    Even innocent business questions that seek de-facto people ranking MUST NOT
    return person-grain data. The data-access layer must reject the query or enforce
    group aggregation.
    """
    with pytest.raises(PersonRankingProhibitedError) as exc:
        enforce_aggregation_barrier(query_text=innocent_query)
    assert "person-grain" in str(exc.value).lower() or "ranking" in str(exc.value).lower()


def test_aggregate_metrics_never_returns_person_grain_rows():
    """
    Structural Shape Barrier:
    Even when raw input data contains individual employee names and records,
    aggregate_metrics() MUST collapse them into team/role roll-ups and NEVER return
    individual employee identities or per-person ranking rows.
    """
    raw_records = [
        {
            "person": "Alice Smith",
            "team": "Field-Ops-Alpha",
            "role": "Technician",
            "resolved": 45,
            "hours": 38.0,
        },
        {
            "person": "Bob Jones",
            "team": "Field-Ops-Alpha",
            "role": "Technician",
            "resolved": 30,
            "hours": 40.0,
        },
        {
            "person": "Charlie Brown",
            "team": "Field-Ops-Beta",
            "role": "Senior Engineer",
            "resolved": 55,
            "hours": 35.0,
        },
    ]

    # Aggregate by team
    aggregated = aggregate_metrics(raw_records, group_by="team", metric_key="resolved")
    assert len(aggregated) == 2

    # Verify return shape has NO person identities
    for row in aggregated:
        assert "person" not in row
        assert "name" not in row
        assert "team" in row
        assert "count" in row
        assert "metric_sum" in row
        assert "metric_avg" in row

    team_alpha = next(r for r in aggregated if r["team"] == "Field-Ops-Alpha")
    assert team_alpha["count"] == 2
    assert team_alpha["metric_sum"] == 75
    assert team_alpha["metric_avg"] == 37.5
