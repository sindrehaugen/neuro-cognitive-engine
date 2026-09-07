"""
tests/unit/test_business_insights_aggregation.py
================================================
Adversarial unit tests for BI-1: Structural Person-Grain Barrier (EU AI Act Article 5).

Legal requirement:
  - Never rank people.
  - Enforced STRUCTURALLY at the data-access layer (aggregation.py).
  - Person-grain rows are NOT a returnable shape for comparison.
  - The board receives aggregates by team, role, department, or period.
  - The ALLOWED_GROUP_DIMENSIONS allowlist strictly decides permitted dimensions.
  - The ask path reaches a structural shape guarantee.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from nce.vertical_modules.business_insights._guard import PersonRankingProhibitedError
from nce.vertical_modules.business_insights.aggregation import (
    ALLOWED_GROUP_DIMENSIONS,
    aggregate_metrics,
    enforce_aggregation_barrier,
    enforce_shape_guarantee,
)
from nce.vertical_modules.business_insights.ask import do_ask_business


def test_aggregation_barrier_rejects_person_grain_grouping():
    """Direct grouping by person, employee, or technician must be structurally rejected."""
    forbidden_dimensions = ["person", "employee", "technician", "user", "individual", "member"]
    for dim in forbidden_dimensions:
        with pytest.raises(PersonRankingProhibitedError) as exc:
            enforce_aggregation_barrier(group_by=dim)
        assert "EU AI Act" in str(exc.value) or "person-grain" in str(exc.value).lower()


def test_aggregation_barrier_rejects_unlisted_and_novel_dimensions():
    """
    Allowlist Decider:
    Any dimension not in ALLOWED_GROUP_DIMENSIONS (including unlisted English nouns
    and Norwegian person terms) MUST be rejected by the barrier.
    """
    novel_unallowed_dimensions = [
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
        "arbitrary_dimension",
        "custom_identifier",
    ]
    for dim in novel_unallowed_dimensions:
        with pytest.raises(PersonRankingProhibitedError) as exc:
            enforce_aggregation_barrier(group_by=dim)
        assert (
            "EU AI Act" in str(exc.value)
            or "person-grain" in str(exc.value).lower()
            or "allowed" in str(exc.value).lower()
        )


def test_aggregation_barrier_accepts_all_allowed_group_dimensions():
    """All dimensions defined in ALLOWED_GROUP_DIMENSIONS must be accepted."""
    for dim in ALLOWED_GROUP_DIMENSIONS:
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
    BI-1 Adversarial Test (Canonical):
    Even innocent business questions that seek de-facto people ranking MUST NOT
    return person-grain data.
    """
    with pytest.raises(PersonRankingProhibitedError) as exc:
        enforce_aggregation_barrier(query_text=innocent_query)
    assert "person-grain" in str(exc.value).lower() or "ranking" in str(exc.value).lower()


@pytest.mark.parametrize(
    "novel_query",
    [
        "who closed the fewest tickets last sprint",
        "show top performing engineers this quarter",
        "compare individual consultants on generated revenue",
        "list technicians sorted by completed tasks",
        "which installer had the lowest margin",
        "hvem har lavest faktureringsgrad denne måneden",
        "ranger montører etter timeantall",
        "vis timer per ansatt forrige måned",
        "hvilken medarbeider solgte mest",
        "vis best ytende utviklere i år",
        "sammenlign individuelle teknikere på oppmøte",
    ],
)
def test_novel_and_norwegian_phrasings_blocked_from_person_grain_return(novel_query: str):
    """
    BI-1 Adversarial Test (Novel & Norwegian):
    Novel English and Norwegian rewordings seeking person-grain ranking must be
    detected and structurally rejected.
    """
    with pytest.raises(PersonRankingProhibitedError) as exc:
        enforce_aggregation_barrier(query_text=novel_query)
    assert (
        "person-grain" in str(exc.value).lower()
        or "ranking" in str(exc.value).lower()
        or "eu ai act" in str(exc.value).lower()
    )


@pytest.mark.parametrize(
    "legitimate_technical_query",
    [
        "hva er status på belysning i prosjektet",
        "oversikt over belysningskontroll",
        "opplysning om generelle driftskostnader",
    ],
)
def test_gate_does_not_false_positive_on_technical_norwegian_terms(
    legitimate_technical_query: str,
):
    """
    False-Positive Guard:
    Legitimate Norwegian queries containing words like 'belysning' or 'opplysning'
    must NOT trigger false-positive person-ranking rejections.
    """
    # Should not raise
    enforce_aggregation_barrier(query_text=legitimate_technical_query)


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


def test_aggregate_metrics_rejects_non_allowed_group_by():
    """aggregate_metrics must reject any group_by not in ALLOWED_GROUP_DIMENSIONS."""
    raw_records = [{"role": "Tech", "val": 10.0}]
    with pytest.raises(PersonRankingProhibitedError):
        aggregate_metrics(raw_records, group_by="engineer", metric_key="val")
    with pytest.raises(PersonRankingProhibitedError):
        aggregate_metrics(raw_records, group_by="montør", metric_key="val")


@pytest.mark.asyncio
async def test_do_ask_business_enforces_shape_guarantee_positive_control():
    """
    Positive Control (U18 / Charter §13 B-BI1):
    Assert a person-grain row cannot appear in the return shape, independent of
    the question asked.
    Even with an innocent, non-ranking question ("what is our overall quarterly delivery volume?"),
    raw input records with individual employee identities MUST be structurally collapsed
    into group-level roll-ups, completely stripping all person names, IDs, and row-grain entities.
    """
    engine = MagicMock()
    engine.pg_pool = None
    engine.pool = None

    raw_person_records = [
        {
            "person": "Alice Smith",
            "employee_id": "EMP-001",
            "team": "Field-Engineering",
            "role": "Lead Tech",
            "tickets": 42,
        },
        {
            "person": "Bob Jones",
            "employee_id": "EMP-002",
            "team": "Field-Engineering",
            "role": "Field Tech",
            "tickets": 28,
        },
        {
            "person": "Charlie Brown",
            "employee_id": "EMP-003",
            "team": "Customer-Success",
            "role": "Consultant",
            "tickets": 35,
        },
    ]

    # Query is entirely innocent and neutral (does NOT match any person ranking pattern)
    params = {
        "namespace_id": "00000000-0000-0000-0000-000000000001",
        "question": "what is our overall quarterly delivery volume across teams?",
        "records": raw_person_records,
        "group_by": "team",
        "metric_key": "tickets",
    }

    result = await do_ask_business(engine, params)
    assert result["status"] == "ok"
    assert "breakdown" in result
    breakdown = result["breakdown"]

    # Structural assertion: breakdown length is 2 (Field-Engineering, Customer-Success)
    assert len(breakdown) == 2

    # Structural assertion: NO person identities or person-grain keys appear anywhere in result
    result_str = json.dumps(result)
    assert "Alice" not in result_str
    assert "Bob" not in result_str
    assert "Charlie" not in result_str
    assert "EMP-001" not in result_str
    assert "EMP-002" not in result_str
    assert "EMP-003" not in result_str

    for row in breakdown:
        assert "person" not in row
        assert "name" not in row
        assert "employee_id" not in row
        assert "team" in row
        assert "count" in row
        assert "metric_sum" in row
        assert "metric_avg" in row

    fe_group = next(r for r in breakdown if r["team"] == "Field-Engineering")
    assert fe_group["count"] == 2
    assert fe_group["metric_sum"] == 70
    assert fe_group["metric_avg"] == 35.0


def test_enforce_shape_guarantee_scrubs_nested_person_grain_data():
    """enforce_shape_guarantee directly strips and aggregates smuggled person grain rows."""
    smuggled_data = {
        "status": "ok",
        "breakdown": [
            {"person": "Eve", "team": "SecOps", "score": 98.0},
            {"person": "Mallory", "team": "SecOps", "score": 92.0},
        ],
    }
    guaranteed = enforce_shape_guarantee(smuggled_data, group_by="team", metric_key="score")
    guaranteed_str = json.dumps(guaranteed)
    assert "Eve" not in guaranteed_str
    assert "Mallory" not in guaranteed_str
    assert "person" not in guaranteed_str
    assert guaranteed["breakdown"][0]["team"] == "SecOps"
    assert guaranteed["breakdown"][0]["count"] == 2
