"""Unit tests for Module 13 HR Engine core domain logic and RL-1 NEVER ranking enforcement."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncpg.exceptions import DataError

from nce.vertical_modules.hr._guard import (
    HrDisabledError,
    HrRankingProhibitedError,
    assert_ranking_prohibited,
    require_hr_enabled,
)
from nce.vertical_modules.hr.profile import (
    do_create_employee,
    do_get_employee,
    do_query_employees,
)
from nce.vertical_modules.hr.skills import (
    do_match_skills,
    do_record_certification,
    do_record_skill,
)
from nce.vertical_modules.hr.taxonomy import (
    get_cert_taxonomy,
    get_skill_taxonomy,
    resolve_implied_skills,
)

_NS_A = "00000000-0000-4000-8000-000000000001"
_NS_B = "00000000-0000-4000-8000-000000000002"


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock(spec=["acquire"])
    pool.acquire.return_value = _AsyncCtx(conn)
    return pool


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace scoped_pg_session with a pass-through for unit tests."""

    @asynccontextmanager
    async def _fake_scoped(pool: Any, ns: Any) -> Any:
        if hasattr(pool, "acquire"):
            ctx = pool.acquire()
            if hasattr(ctx, "__aenter__"):
                conn = await ctx.__aenter__()
                try:
                    yield conn
                finally:
                    await ctx.__aexit__(None, None, None)
            else:
                yield ctx
        else:
            yield pool

    monkeypatch.setattr(
        "nce.vertical_modules.hr.profile.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.hr.skills.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )


# =========================================================================
# 1. RL-1 NEVER Ranking Enforcement
# =========================================================================


def test_rl1_ranking_prohibited_refuses_leaderboard():
    """RL-1: Calls requesting standing rankings or leaderboards must be refused."""
    with pytest.raises(HrRankingProhibitedError, match="NEVER ranking policy"):
        assert_ranking_prohibited({"leaderboard": True})

    with pytest.raises(HrRankingProhibitedError, match="NEVER ranking policy"):
        assert_ranking_prohibited({"standing_ranking": True})

    with pytest.raises(HrRankingProhibitedError, match="NEVER ranking policy"):
        assert_ranking_prohibited({"rank_employees": True})

    with pytest.raises(HrRankingProhibitedError, match="NEVER ranking policy"):
        assert_ranking_prohibited({"compare_peers": True})


def test_rl1_ranking_prohibited_sort_by():
    """RL-1: Refuse sort_by rating/score/performance."""
    for sort_key in ("rating", "score", "performance", "standing", "rank"):
        with pytest.raises(HrRankingProhibitedError, match="Sorting candidates"):
            assert_ranking_prohibited({"sort_by": sort_key})


# =========================================================================
# 2. Opt-in Guard (require_hr_enabled)
# =========================================================================


@pytest.mark.asyncio
async def test_require_hr_enabled_success():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"hr_enabled": True}
    pool = _make_mock_pool(conn)

    await require_hr_enabled(pool, _NS_A)
    conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_require_hr_enabled_disabled():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"hr_enabled": False}
    pool = _make_mock_pool(conn)

    with pytest.raises(HrDisabledError, match="has not enabled the HR Engine"):
        await require_hr_enabled(pool, _NS_A)


@pytest.mark.asyncio
async def test_require_hr_enabled_invalid_uuid():
    conn = AsyncMock()
    conn.fetchrow.side_effect = DataError("invalid input syntax for type uuid")
    pool = _make_mock_pool(conn)

    with pytest.raises(HrDisabledError, match="invalid or has not enabled HR"):
        await require_hr_enabled(pool, "invalid-uuid")


# =========================================================================
# 3. Taxonomy
# =========================================================================


def test_taxonomy_implied_skills():
    """Verify taxonomy resolves cert-to-skill implications."""
    implied = resolve_implied_skills(["CTS-D", "Q-SYS Level 2"])
    assert "AV System Design" in implied
    assert "Q-SYS Core Commissioning" in implied


def test_taxonomy_retrieval():
    """Verify embedded skill and cert taxonomies are populated without external config files."""
    skills_map = get_skill_taxonomy()
    certs_map = get_cert_taxonomy()
    assert "audio" in skills_map
    assert "Dante routing" in skills_map["audio"]["skills"]
    assert "CTS" in certs_map
    assert "CTS-D" in certs_map


# =========================================================================
# 4. Profile Management (do_create_employee, do_get_employee, do_query_employees)
# =========================================================================


@pytest.mark.asyncio
async def test_do_create_employee_success():
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetchrow.return_value = {
        "id": "11111111-1111-4000-8000-111111111111",
        "employee_id": "EMP-ALPHA",
        "namespace_id": _NS_A,
        "name": "Employee Alpha",
        "email": "emp.alpha@example.test",
        "role": "technician",
        "department": "operations",
        "location_id": "OSLO-HQ",
        "leave_balance": 25.0,
        "active": True,
        "hr_source_id": "SRC-ALPHA-01",
        "created_at": now_dt,
        "updated_at": now_dt,
    }
    pool = _make_mock_pool(conn)

    res = await do_create_employee(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "name": "Employee Alpha",
            "email": "emp.alpha@example.test",
            "location_id": "OSLO-HQ",
            "hr_source_id": "SRC-ALPHA-01",
        },
    )

    assert res["employee_id"] == "EMP-ALPHA"
    assert res["name"] == "Employee Alpha"
    assert res["leave_balance"] == 25.0
    assert res["hr_source_id"] == "SRC-ALPHA-01"


@pytest.mark.asyncio
async def test_do_create_employee_validation():
    pool = MagicMock()
    with pytest.raises(ValueError, match="Invalid UUID"):
        await do_create_employee(
            pool, {"namespace_id": "bad-uuid", "employee_id": "E1", "name": "Test"}
        )

    with pytest.raises(ValueError, match="employee_id is required"):
        await do_create_employee(pool, {"namespace_id": _NS_A, "name": "Test"})

    with pytest.raises(ValueError, match="name is required"):
        await do_create_employee(pool, {"namespace_id": _NS_A, "employee_id": "E1"})


@pytest.mark.asyncio
async def test_do_get_employee_scoping_privileged():
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetchrow.return_value = {
        "id": "11111111-1111-4000-8000-111111111111",
        "employee_id": "EMP-ALPHA",
        "namespace_id": _NS_A,
        "name": "Employee Alpha",
        "email": "emp.alpha@example.test",
        "role": "technician",
        "department": "operations",
        "location_id": "OSLO-HQ",
        "leave_balance": 21.5,
        "active": True,
        "hr_source_id": "SRC-ALPHA-01",
        "raw": json.dumps({"notes": "Internal notes"}),
        "created_at": now_dt,
        "updated_at": now_dt,
    }
    conn.fetch.side_effect = [
        [
            {
                "skill_id": "dante-routing",
                "name": "Dante Network Routing",
                "category": "audio",
                "level": "expert",
                "assessed_at": now_dt,
            }
        ],
        [
            {
                "cert_id": "cts",
                "authority": "AVIXA",
                "name": "CTS",
                "issued": now_dt,
                "valid_to": now_dt,
                "status": "active",
            }
        ],
    ]
    pool = _make_mock_pool(conn)

    # Privileged caller: manager
    card = await do_get_employee(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "caller_role": "manager",
        },
    )

    assert card["employee_id"] == "EMP-ALPHA"
    assert card["leave_balance"] == 21.5
    assert card["hr_source_id"] == "SRC-ALPHA-01"
    assert "raw" in card
    assert len(card["skills"]) == 1
    assert len(card["certifications"]) == 1


@pytest.mark.asyncio
async def test_do_get_employee_scoping_peer():
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetchrow.return_value = {
        "id": "11111111-1111-4000-8000-111111111111",
        "employee_id": "EMP-ALPHA",
        "namespace_id": _NS_A,
        "name": "Employee Alpha",
        "email": "emp.alpha@example.test",
        "role": "technician",
        "department": "operations",
        "location_id": "OSLO-HQ",
        "leave_balance": 21.5,
        "active": True,
        "hr_source_id": "SRC-ALPHA-01",
        "raw": json.dumps({"notes": "Internal notes"}),
        "created_at": now_dt,
        "updated_at": now_dt,
    }
    conn.fetch.side_effect = [[], []]
    pool = _make_mock_pool(conn)

    # Peer caller: unprivileged
    card = await do_get_employee(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "caller_role": "peer",
            "caller_id": "EMP-BETA",
        },
    )

    assert card["employee_id"] == "EMP-ALPHA"
    assert "leave_balance" not in card  # Redacted for peer!
    assert "hr_source_id" not in card  # Redacted for peer!
    assert "raw" not in card  # Redacted for peer!


@pytest.mark.asyncio
async def test_do_get_employee_self_scoping():
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetchrow.return_value = {
        "id": "11111111-1111-4000-8000-111111111111",
        "employee_id": "EMP-ALPHA",
        "namespace_id": _NS_A,
        "name": "Employee Alpha",
        "email": "emp.alpha@example.test",
        "role": "technician",
        "department": "operations",
        "location_id": "OSLO-HQ",
        "leave_balance": 21.5,
        "active": True,
        "hr_source_id": "SRC-ALPHA-01",
        "raw": {"notes": "Internal notes"},
        "created_at": now_dt,
        "updated_at": now_dt,
    }
    conn.fetch.side_effect = [[], []]
    pool = _make_mock_pool(conn)

    # Self caller: caller_id == employee_id
    card = await do_get_employee(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "caller_id": "EMP-ALPHA",
            "caller_role": "peer",
        },
    )

    assert card["employee_id"] == "EMP-ALPHA"
    assert card["leave_balance"] == 21.5
    assert card["hr_source_id"] == "SRC-ALPHA-01"


@pytest.mark.asyncio
async def test_do_query_employees_filter_and_scoping():
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetch.return_value = [
        {
            "id": "11111111-1111-4000-8000-111111111111",
            "employee_id": "EMP-ALPHA",
            "namespace_id": _NS_A,
            "name": "Employee Alpha",
            "email": "emp.alpha@example.test",
            "role": "technician",
            "department": "operations",
            "location_id": "OSLO-HQ",
            "leave_balance": 25.0,
            "active": True,
            "created_at": now_dt,
        }
    ]
    pool = _make_mock_pool(conn)

    # 1. Peer query
    res_peer = await do_query_employees(
        pool,
        {
            "namespace_id": _NS_A,
            "department": "operations",
            "caller_role": "peer",
        },
    )
    assert res_peer["count"] == 1
    assert "leave_balance" not in res_peer["employees"][0]

    # 2. Admin query
    res_admin = await do_query_employees(
        pool,
        {
            "namespace_id": _NS_A,
            "department": "operations",
            "caller_role": "admin",
        },
    )
    assert res_admin["count"] == 1
    assert res_admin["employees"][0]["leave_balance"] == 25.0


# =========================================================================
# 5. Skills & Certifications Recording
# =========================================================================


@pytest.mark.asyncio
async def test_do_record_skill():
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetchrow.return_value = {
        "id": "22222222-2222-4000-8000-222222222222",
        "skill_id": "dante-routing",
        "employee_id": "EMP-ALPHA",
        "namespace_id": _NS_A,
        "name": "Dante Routing",
        "category": "audio",
        "level": "expert",
        "assessed_at": now_dt,
        "hr_source_id": "SRC-SKILL-01",
    }
    pool = _make_mock_pool(conn)

    res = await do_record_skill(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "skill_id": "dante-routing",
            "name": "Dante Routing",
            "category": "audio",
            "level": "expert",
            "hr_source_id": "SRC-SKILL-01",
        },
    )

    assert res["skill_id"] == "dante-routing"
    assert res["level"] == "expert"
    assert res["hr_source_id"] == "SRC-SKILL-01"


@pytest.mark.asyncio
async def test_do_record_certification():
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetchrow.return_value = {
        "id": "33333333-3333-4000-8000-333333333333",
        "cert_id": "cts",
        "employee_id": "EMP-ALPHA",
        "namespace_id": _NS_A,
        "authority": "AVIXA",
        "name": "CTS",
        "issued": now_dt,
        "valid_to": now_dt,
        "status": "active",
        "hr_source_id": "SRC-CERT-01",
    }
    pool = _make_mock_pool(conn)

    res = await do_record_certification(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "cert_id": "cts",
            "authority": "AVIXA",
            "name": "CTS",
            "status": "active",
            "hr_source_id": "SRC-CERT-01",
        },
    )

    assert res["cert_id"] == "cts"
    assert res["authority"] == "AVIXA"
    assert res["status"] == "active"


@pytest.mark.asyncio
async def test_do_record_certification_date_coercion():
    """do_record_certification must parse date strings into date/datetime objects and validate malformed dates."""
    conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    conn.fetchrow.return_value = {
        "id": "33333333-3333-4000-8000-333333333333",
        "cert_id": "cts",
        "employee_id": "EMP-ALPHA",
        "namespace_id": _NS_A,
        "authority": "AVIXA",
        "name": "CTS",
        "issued": now_dt,
        "valid_to": now_dt,
        "status": "active",
        "hr_source_id": "SRC-CERT-01",
    }
    pool = _make_mock_pool(conn)

    # 1. With ISO strings: should bind datetime.date objects to conn.fetchrow, NOT str
    await do_record_certification(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "cert_id": "cts",
            "authority": "AVIXA",
            "name": "CTS",
            "issued": "2026-09-01",
            "valid_to": "2029-09-01",
        },
    )
    bound_args = conn.fetchrow.call_args[0]
    bound_issued = bound_args[6]
    bound_valid_to = bound_args[7]
    assert isinstance(bound_issued, (date, datetime)), (
        f"Expected date/datetime for issued, got {type(bound_issued)}"
    )
    assert isinstance(bound_valid_to, (date, datetime)), (
        f"Expected date/datetime for valid_to, got {type(bound_valid_to)}"
    )

    # 2. With real date objects: both forms must work
    d_issued = date(2026, 9, 1)
    d_valid = date(2029, 9, 1)
    await do_record_certification(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "cert_id": "cts",
            "authority": "AVIXA",
            "name": "CTS",
            "issued": d_issued,
            "valid_to": d_valid,
        },
    )
    bound_args_dt = conn.fetchrow.call_args[0]
    assert bound_args_dt[6] == d_issued
    assert bound_args_dt[7] == d_valid

    # 3. Malformed date raises clean ValueError, not asyncpg DataError
    with pytest.raises(ValueError, match="Invalid ISO date for issued"):
        await do_record_certification(
            pool,
            {
                "namespace_id": _NS_A,
                "employee_id": "EMP-ALPHA",
                "cert_id": "cts",
                "authority": "AVIXA",
                "name": "CTS",
                "issued": "not-a-date",
            },
        )

    with pytest.raises(ValueError, match="Invalid ISO date for valid_to"):
        await do_record_certification(
            pool,
            {
                "namespace_id": _NS_A,
                "employee_id": "EMP-ALPHA",
                "cert_id": "cts",
                "authority": "AVIXA",
                "name": "CTS",
                "valid_to": "not-a-valid-date",
            },
        )

    # 4. Default issued (when omitted) must be a date, and valid_to omitted/None must be None
    await do_record_certification(
        pool,
        {
            "namespace_id": _NS_A,
            "employee_id": "EMP-ALPHA",
            "cert_id": "cts",
            "authority": "AVIXA",
            "name": "CTS",
            "valid_to": None,
        },
    )
    bound_args2 = conn.fetchrow.call_args[0]
    assert isinstance(bound_args2[6], (date, datetime)), (
        f"Expected date/datetime for default issued, got {type(bound_args2[6])}"
    )
    assert bound_args2[7] is None, f"Expected None for omitted valid_to, got {bound_args2[7]}"


# =========================================================================
# 6. Skills Matching (do_match_skills) & RL-1 NEVER Ranking Invariant
# =========================================================================


@pytest.mark.asyncio
async def test_do_match_skills_pure_fit_not_standing_rank():
    """do_match_skills returns requirement fit for eligible candidates, not a global ranking."""
    candidates = [
        {
            "employee_id": "EMP-ALPHA",
            "name": "Employee Alpha",
            "skills": ["Dante routing", "Cabling"],
            "certs": ["CTS"],
        },
        {
            "employee_id": "EMP-BETA",
            "name": "Employee Beta",
            "skills": ["Cabling"],
            "certs": [],
        },
    ]

    result = await do_match_skills(
        None,
        {
            "namespace_id": _NS_A,
            "required_skills": ["Dante routing"],
            "required_certs": ["CTS"],
            "candidates": candidates,
        },
    )

    assert "eligible_set" in result
    eligible = result["eligible_set"]
    assert len(eligible) == 1
    assert eligible[0]["employee_id"] == "EMP-ALPHA"
    assert eligible[0]["eligible"] is True
    assert "score" not in eligible[0]  # No comparative standing scores!


@pytest.mark.asyncio
async def test_do_match_skills_with_db_fallback():
    conn = AsyncMock()
    conn.fetch.side_effect = [
        # 1. Employees
        [
            {"employee_id": "EMP-ALPHA", "name": "Employee Alpha"},
            {"employee_id": "EMP-BETA", "name": "Employee Beta"},
        ],
        # 2. Skills for ALPHA
        [{"name": "Dante Routing"}],
        # 3. Certs for ALPHA (CTS-D implies AV System Design)
        [{"name": "CTS-D"}],
        # 4. Skills for BETA
        [{"name": "Cabling"}],
        # 5. Certs for BETA
        [],
    ]
    pool = _make_mock_pool(conn)

    res = await do_match_skills(
        pool,
        {
            "namespace_id": _NS_A,
            "required_skills": ["AV System Design"],  # Implied from CTS-D!
            "required_certs": ["CTS-D"],
        },
    )

    assert len(res["eligible_set"]) == 1
    match_alpha = res["eligible_set"][0]
    assert match_alpha["employee_id"] == "EMP-ALPHA"
    assert "AV System Design" in match_alpha["matched_skills"]
    assert "score" not in match_alpha


@pytest.mark.asyncio
async def test_do_match_skills_refuses_ranking():
    """do_match_skills refuses ranking requests directly."""
    with pytest.raises(HrRankingProhibitedError):
        await do_match_skills(
            None,
            {
                "namespace_id": _NS_A,
                "standing_ranking": True,
            },
        )
