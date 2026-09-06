"""
tests/unit/test_hr_c9b_guard.py
===============================
Verification suite for Wave HR-3: EU AI Act Art. 5 C9b structural guard in HR.

Asserts:
  - HrRankingProhibitedError is a subclass of PersonGrainRejected.
  - assert_ranking_prohibited rejects adversarial inputs absent from the brief:
    - sort_by / order_by / rank_by with utilization, fit, workload, score, rating, etc.
    - top_n, top_k, limit_top, best_of
    - boolean and string truthy flags (rank, ranking, compare, comparison, leaderboard)
  - do_capacity enforces assert_ranking_prohibited against adversarial ranking attempts.
  - do_match_skills enforces assert_ranking_prohibited and returns unranked eligible sets.
  - handle_hr_capacity and handle_hr_match_skills map refusals to MCP_SCOPE_FORBIDDEN.
  - api_hr_capacity and api_hr_match_skills map refusals to HTTP 403.
  - Compliant queries pass cleanly.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.structural.no_person_grain import (
    AggregationGrain,
    PersonGrainRejected,
)
from nce.vertical_modules.hr._guard import (
    HrRankingProhibitedError,
    assert_ranking_prohibited,
)
from nce.vertical_modules.hr.capacity import do_capacity
from nce.vertical_modules.hr.mcp_handlers import (
    handle_hr_capacity,
    handle_hr_match_skills,
)
from nce.vertical_modules.hr.skills import do_match_skills

# =========================================================================
# 1. Structural Class Hierarchy (C9b Contract)
# =========================================================================


def test_hr_ranking_prohibited_is_subclass_of_person_grain_rejected() -> None:
    """EU AI Act Art. 5 structural floor: HrRankingProhibitedError is a PersonGrainRejected."""
    assert issubclass(HrRankingProhibitedError, PersonGrainRejected)
    err = HrRankingProhibitedError("Ranking is prohibited")
    assert isinstance(err, PersonGrainRejected)
    assert isinstance(err, HrRankingProhibitedError)


# =========================================================================
# 2. Adversarial Inputs Absent from Brief (Direct Guard Check)
# =========================================================================


@pytest.mark.parametrize(
    "adversarial_params",
    [
        {"sort_by": "utilization_pct"},
        {"sort_by": "utilization_pct DESC"},
        {"sort_by": "workload"},
        {"sort_by": "fit_score"},
        {"sort_by": "fit"},
        {"order_by": "score DESC"},
        {"order_by": "utilization"},
        {"order_by": "performance ASC"},
        {"order_by": "standing"},
        {"rank_by": "fit"},
        {"rank_by": "skills"},
        {"top_n": 3},
        {"top_k": 5},
        {"limit_top": 10},
        {"best_of": 5},
        {"rank": True},
        {"rank": "true"},
        {"rank": 1},
        {"ranking": "relative"},
        {"ranking": True},
        {"compare": True},
        {"compare": "individual"},
        {"comparison": True},
        {"is_comparison_or_ranking": True},
        {"leaderboard": "true"},
        {"leaderboard": 1},
        {"compare_peers": 1},
        {"compare_peers": "true"},
    ],
)
def test_assert_ranking_prohibited_rejects_adversarial_inputs(
    adversarial_params: dict,
) -> None:
    """Verify that ranking/comparison attempts absent from the brief raise both HrRankingProhibitedError and PersonGrainRejected."""
    with pytest.raises(PersonGrainRejected) as exc_info:
        assert_ranking_prohibited(adversarial_params)

    assert isinstance(exc_info.value, HrRankingProhibitedError)
    assert "RL-1 / C9b" in str(exc_info.value)
    assert "EU AI Act Art. 5" in str(exc_info.value)


def test_assert_ranking_prohibited_allows_compliant_params() -> None:
    """Verify that compliant non-ranking parameters pass without raising."""
    compliant_cases = [
        {},
        {"employee_id": "EMP-001"},
        {"department": "Operations"},
        {"horizon_days": 30},
        {"sort_by": "name"},
        {"sort_by": "employee_id"},
        {"required_skills": ["python", "dante"]},
        {"requested_grain": AggregationGrain.PERSON},
        {"grain": "team", "sort_by": "utilization"},  # safe grain: TEAM allows comparison
    ]
    for case in compliant_cases:
        assert_ranking_prohibited(case)


# =========================================================================
# 3. do_capacity Enforcement
# =========================================================================


@pytest.mark.asyncio
async def test_do_capacity_refuses_ranking_and_sorting() -> None:
    """do_capacity must enforce assert_ranking_prohibited before touching data."""
    mock_engine = MagicMock()
    ns_id = str(uuid4())

    adversarial_cases = [
        {"namespace_id": ns_id, "sort_by": "utilization"},
        {"namespace_id": ns_id, "sort_by": "utilization_pct DESC"},
        {"namespace_id": ns_id, "order_by": "workload"},
        {"namespace_id": ns_id, "leaderboard": True},
        {"namespace_id": ns_id, "top_n": 5},
        {"namespace_id": ns_id, "rank": True},
    ]

    for params in adversarial_cases:
        with pytest.raises(PersonGrainRejected) as exc_info:
            await do_capacity(mock_engine, params)
        assert isinstance(exc_info.value, HrRankingProhibitedError)


@pytest.mark.asyncio
async def test_do_capacity_compliant_execution() -> None:
    """do_capacity computes capacity when called with compliant parameters."""
    ns_uuid = uuid4()
    mock_engine = MagicMock()
    mock_pool = MagicMock()
    mock_engine.pg_pool = mock_pool

    mock_conn = AsyncMock()
    # Mock employee row
    mock_conn.fetch.side_effect = [
        [{"employee_id": "EMP-001", "name": "Alice", "department": "AV", "role": "Tech"}],
        [],  # work_orders
        [],  # absences
    ]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_scoped(pool, ns):
        yield mock_conn

    with patch("nce.vertical_modules.hr.capacity.scoped_pg_session", _fake_scoped):
        res = await do_capacity(
            mock_engine,
            {"namespace_id": str(ns_uuid), "department": "AV", "horizon_days": 14},
        )

    assert res["namespace_id"] == str(ns_uuid)
    assert res["count"] == 1
    assert res["capacities"][0]["employee_id"] == "EMP-001"
    assert res["capacities"][0]["utilization_pct"] == 0.0


# =========================================================================
# 4. do_match_skills Enforcement
# =========================================================================


@pytest.mark.asyncio
async def test_do_match_skills_refuses_adversarial_ranking() -> None:
    """do_match_skills must refuse ranking parameters and top_n limits."""
    mock_engine = MagicMock()
    ns_id = str(uuid4())

    adversarial_cases = [
        {"namespace_id": ns_id, "sort_by": "fit"},
        {"namespace_id": ns_id, "rank_by": "score"},
        {"namespace_id": ns_id, "top_k": 3},
        {"namespace_id": ns_id, "compare_peers": True},
        {"namespace_id": ns_id, "ranking": "relative"},
    ]

    for params in adversarial_cases:
        with pytest.raises(PersonGrainRejected) as exc_info:
            await do_match_skills(mock_engine, params)
        assert isinstance(exc_info.value, HrRankingProhibitedError)


@pytest.mark.asyncio
async def test_do_match_skills_returns_unranked_eligible_set() -> None:
    """do_match_skills returns an unranked eligible set without score or ranking fields."""
    ns_uuid = uuid4()
    mock_engine = MagicMock()

    candidates = [
        {
            "employee_id": "EMP-001",
            "name": "Alice",
            "skills": ["dante-routing", "python"],
            "certs": ["CTS-D"],
        },
        {
            "employee_id": "EMP-002",
            "name": "Bob",
            "skills": ["qsys-core"],
            "certs": [],
        },
    ]

    res = await do_match_skills(
        mock_engine,
        {
            "namespace_id": str(ns_uuid),
            "required_skills": ["dante-routing"],
            "candidates": candidates,
        },
    )

    assert res["namespace_id"] == str(ns_uuid)
    assert res["eligible_count"] == 1
    eligible = res["eligible_set"][0]
    assert eligible["employee_id"] == "EMP-001"
    assert eligible["eligible"] is True
    # Ensure no ranking, score, or percentile fields exist
    assert "score" not in eligible
    assert "rank" not in eligible
    assert "percentile" not in eligible


# =========================================================================
# 5. MCP Handler Exception Mapping (MCP_SCOPE_FORBIDDEN)
# =========================================================================


@pytest.mark.asyncio
async def test_handle_hr_capacity_maps_ranking_refusal_to_forbidden() -> None:
    """handle_hr_capacity raises McpError(MCP_SCOPE_FORBIDDEN) when ranking is attempted."""
    mock_engine = MagicMock()
    ns_id = str(uuid4())

    with patch(
        "nce.vertical_modules.hr.mcp_handlers._check_hr_enabled", AsyncMock(return_value=ns_id)
    ):
        for bad_param in ({"sort_by": "utilization"}, {"leaderboard": True}, {"top_n": 3}):
            args = {"namespace_id": ns_id, **bad_param}
            with pytest.raises(McpError) as exc_info:
                await handle_hr_capacity(mock_engine, args)
            assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
            assert "EU AI Act Art. 5" in exc_info.value.message


@pytest.mark.asyncio
async def test_handle_hr_match_skills_maps_ranking_refusal_to_forbidden() -> None:
    """handle_hr_match_skills raises McpError(MCP_SCOPE_FORBIDDEN) when ranking is attempted."""
    mock_engine = MagicMock()
    ns_id = str(uuid4())

    with patch(
        "nce.vertical_modules.hr.mcp_handlers._check_hr_enabled", AsyncMock(return_value=ns_id)
    ):
        for bad_param in ({"sort_by": "fit"}, {"rank": True}, {"top_k": 5}):
            args = {"namespace_id": ns_id, **bad_param}
            with pytest.raises(McpError) as exc_info:
                await handle_hr_match_skills(mock_engine, args)
            assert exc_info.value.code == MCP_SCOPE_FORBIDDEN
            assert "EU AI Act Art. 5" in exc_info.value.message


# =========================================================================
# 6. REST Route Exception Mapping (HTTP 403)
# =========================================================================


@pytest.mark.asyncio
async def test_api_hr_capacity_returns_403_on_ranking() -> None:
    """api_hr_capacity returns HTTP 403 when query parameters attempt ranking or sorting."""
    from nce.admin_handlers._shared import admin_state
    from nce.admin_handlers.hr import api_hr_capacity

    mock_engine = MagicMock()
    admin_state.engine = mock_engine
    ns_id = str(uuid4())

    with patch("nce.admin_handlers.hr.require_hr_enabled", AsyncMock()):
        mock_req = MagicMock()
        mock_req.query_params = {"namespace_id": ns_id, "sort_by": "utilization_pct"}

        resp = await api_hr_capacity(mock_req)
        assert resp.status_code == 403
        data = json.loads(resp.body)
        assert "EU AI Act Art. 5" in data["error"]


@pytest.mark.asyncio
async def test_api_hr_match_skills_returns_403_on_ranking() -> None:
    """api_hr_match_skills returns HTTP 403 when payload attempts ranking or sorting."""
    from nce.admin_handlers._shared import admin_state
    from nce.admin_handlers.hr import api_hr_match_skills

    mock_engine = MagicMock()
    admin_state.engine = mock_engine
    ns_id = str(uuid4())

    with patch("nce.admin_handlers.hr.require_hr_enabled", AsyncMock()):
        mock_req = MagicMock()
        mock_req.query_params = {}
        mock_req.json = AsyncMock(return_value={"namespace_id": ns_id, "sort_by": "score"})

        resp = await api_hr_match_skills(mock_req)
        assert resp.status_code == 403
        data = json.loads(resp.body)
        assert "EU AI Act Art. 5" in data["error"]
