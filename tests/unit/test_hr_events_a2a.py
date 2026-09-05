"""
tests/unit/test_hr_events_a2a.py
================================
Unit tests for Module 13 (HR Engine) Phase 7:
Events, Replay Handlers, Producer Coverage, and A2A interfaces.

Verifies:
  1. HR Event types in EventType, VALID_EVENT_TYPES, and EVENT_REQUIRED_PARAM_KEYS.
  2. ForkedReplay handler registry coverage and provenance-only execution for HR events.
  3. Producer coverage: string literals present in HR module producers.
  4. handle_project_assignment_query: skills/certs match, capacity, KG edge write.
  5. handle_field_tech_dispatch_query: cert status, load, eligibility rationale.
  6. handle_vendor_contractor_skill_align: canonical taxonomy mapping and implied skills.
  7. get_morning_brief_hr_slice: aggregate operational risk only (no individual ranking).
  8. RL-1 guard enforcement across all A2A entry points.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import get_args
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.event_types import (
    EVENT_FORBIDDEN_PARAM_KEYS,
    EVENT_REQUIRED_PARAM_KEYS,
    VALID_EVENT_TYPES,
    EventType,
)
from nce.replay import (
    _HANDLER_REGISTRY,
    _EventRow,
    _validate_handler_coverage,
)
from nce.vertical_modules.hr._guard import HrRankingProhibitedError
from nce.vertical_modules.hr.a2a import (
    get_morning_brief_hr_slice,
    handle_field_tech_dispatch_query,
    handle_project_assignment_query,
    handle_vendor_contractor_skill_align,
)
from nce.vertical_modules.hr.absences import EVENT_TYPE_HR_ABSENCE_REGISTERED
from nce.vertical_modules.hr.compliance import EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED
from nce.vertical_modules.hr.onboarding import EVENT_TYPE_HR_QUEST_PROGRESSED
from nce.vertical_modules.hr.profile import EVENT_TYPE_HR_EMPLOYEE_CREATED

_HR_EVENT_TYPES = frozenset(
    {
        "hr_employee_created",
        "hr_absence_registered",
        "hr_compliance_milestone_recorded",
        "hr_quest_progressed",
    }
)


# ---------------------------------------------------------------------------
# 1. Event types definition and contracts
# ---------------------------------------------------------------------------


def test_hr_event_types_in_event_type_union() -> None:
    """All 4 HR event types must be members of EventType and VALID_EVENT_TYPES."""
    all_types = frozenset(get_args(EventType))
    missing = _HR_EVENT_TYPES - all_types
    assert not missing, f"EventType missing HR event types: {sorted(missing)}"

    missing_valid = _HR_EVENT_TYPES - VALID_EVENT_TYPES
    assert not missing_valid, f"VALID_EVENT_TYPES missing: {sorted(missing_valid)}"


def test_hr_event_required_param_keys() -> None:
    """Every HR event type must declare its required param keys."""
    for et in _HR_EVENT_TYPES:
        assert et in EVENT_REQUIRED_PARAM_KEYS, f"{et} missing from EVENT_REQUIRED_PARAM_KEYS"
        req_keys = EVENT_REQUIRED_PARAM_KEYS[et]
        assert len(req_keys) >= 1, f"{et} has empty required param keys"

    assert "employee_id" in EVENT_REQUIRED_PARAM_KEYS["hr_employee_created"]
    assert "absence_id" in EVENT_REQUIRED_PARAM_KEYS["hr_absence_registered"]
    assert "milestone" in EVENT_REQUIRED_PARAM_KEYS["hr_compliance_milestone_recorded"]
    assert "progress_pct" in EVENT_REQUIRED_PARAM_KEYS["hr_quest_progressed"]


def test_hr_event_forbidden_param_keys_enforce_rl1() -> None:
    """Forbidden param keys in EVENT_FORBIDDEN_PARAM_KEYS must prevent ranking in audit log."""
    for et in _HR_EVENT_TYPES:
        assert et in EVENT_FORBIDDEN_PARAM_KEYS, f"{et} missing from EVENT_FORBIDDEN_PARAM_KEYS"
        forbidden = EVENT_FORBIDDEN_PARAM_KEYS[et]
        assert "ranking" in forbidden
        assert "score" in forbidden


# ---------------------------------------------------------------------------
# 2. Replay handlers coverage and execution
# ---------------------------------------------------------------------------


def test_hr_replay_handler_coverage() -> None:
    """_validate_handler_coverage must pass and all HR event types must be registered."""
    _validate_handler_coverage()
    for et in _HR_EVENT_TYPES:
        assert et in _HANDLER_REGISTRY, f"No replay handler registered for {et!r}"


@pytest.mark.asyncio
async def test_hr_replay_handler_execution() -> None:
    """Invoking the registered replay handler for HR events returns provenance-only dict."""
    conn = MagicMock()
    ctx = uuid4()
    for et in _HR_EVENT_TYPES:
        handler = _HANDLER_REGISTRY[et]
        row = _EventRow(
            event_id=uuid4(),
            event_seq=101,
            event_type=et,
            occurred_at=datetime.now(timezone.utc),
            agent_id="hr-admin-agent",
            params={"employee_id": "EMP-001", "role": "Technician"},
            result_summary=None,
            parent_event_id=None,
            llm_payload_uri=None,
            llm_payload_hash=None,
        )
        result = await handler(conn, row, ctx, None, None)
        assert result.get("replayed") is True
        assert result.get("event_type") == et


# ---------------------------------------------------------------------------
# 3. Producers verification
# ---------------------------------------------------------------------------


def test_hr_event_constants_match_event_types() -> None:
    """Constants exported from HR vertical modules must match declared EventTypes."""
    assert EVENT_TYPE_HR_EMPLOYEE_CREATED == "hr_employee_created"
    assert EVENT_TYPE_HR_ABSENCE_REGISTERED == "hr_absence_registered"
    assert EVENT_TYPE_HR_COMPLIANCE_MILESTONE_RECORDED == "hr_compliance_milestone_recorded"
    assert EVENT_TYPE_HR_QUEST_PROGRESSED == "hr_quest_progressed"


# ---------------------------------------------------------------------------
# 4. A2A Project Assignment Flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_assignment_query_match_and_assign() -> None:
    """Project assignment queries candidates and writes graph edges upon confirmation."""
    ns_id = uuid4()
    prj_id = "PRJ-ALPHA-101"

    mock_engine = MagicMock()
    mock_pool = MagicMock()
    mock_engine.pg_pool = mock_pool

    mock_conn = AsyncMock()

    # Context manager for scoped_pg_session
    class FakeSession:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, *args):
            pass

    from unittest.mock import patch

    with (
        patch("nce.vertical_modules.hr.a2a.scoped_pg_session", return_value=FakeSession()),
        patch(
            "nce.vertical_modules.hr.a2a.do_match_skills",
            new_callable=AsyncMock,
        ) as mock_match,
        patch(
            "nce.vertical_modules.hr.a2a.do_capacity",
            new_callable=AsyncMock,
        ) as mock_cap,
    ):
        mock_match.return_value = {
            "candidates": [
                {
                    "employee_id": "EMP-100",
                    "name": "Candidate Alpha",
                    "role": "Project Manager",
                    "skills_matched": ["AV System Design", "Site commissioning"],
                    "certs_matched": ["CTS-D"],
                }
            ]
        }
        mock_cap.return_value = {
            "employee_id": "EMP-100",
            "utilization_pct": 50.0,
            "period": {"days": 30},
        }

        # Mock employee exists
        mock_conn.fetchrow.return_value = {
            "id": uuid4(),
            "employee_id": "EMP-100",
        }

        # 1. Query candidates without assigning
        res = await handle_project_assignment_query(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "project_id": prj_id,
                "required_skills": ["AV System Design"],
                "required_certs": ["CTS-D"],
            },
        )
        assert res["project_id"] == prj_id
        assert res["candidate_count"] == 1
        cand = res["candidates"][0]
        assert cand["employee_id"] == "EMP-100"
        assert cand["utilization_pct"] == 50.0
        assert cand["available"] is True
        assert "assigned_lead" not in res

        # 2. Confirm assignment
        res_assigned = await handle_project_assignment_query(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "project_id": prj_id,
                "required_skills": ["AV System Design"],
                "assign_lead_id": "EMP-100",
            },
        )
        assert "assigned_lead" in res_assigned
        assert res_assigned["assigned_lead"]["status"] == "confirmed"
        assert (
            res_assigned["assigned_lead"]["edge"]
            == f"PROJECT:{prj_id} -[led_by]-> EMPLOYEE:EMP-100"
        )

        # Verify DB execute calls for KG nodes and edges
        assert mock_conn.execute.call_count >= 3


@pytest.mark.asyncio
async def test_handle_project_assignment_query_unknown_employee_raises() -> None:
    """Attempting to assign a non-existent employee raises ValueError."""
    ns_id = uuid4()
    mock_engine = MagicMock()
    mock_conn = AsyncMock()

    class FakeSession:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, *args):
            pass

    from unittest.mock import patch

    with (
        patch("nce.vertical_modules.hr.a2a.scoped_pg_session", return_value=FakeSession()),
        patch(
            "nce.vertical_modules.hr.a2a.do_match_skills",
            new_callable=AsyncMock,
        ) as mock_match,
        patch(
            "nce.vertical_modules.hr.a2a.do_capacity",
            new_callable=AsyncMock,
        ) as mock_cap,
    ):
        mock_match.return_value = {"candidates": []}
        mock_cap.return_value = {"utilization_pct": 0.0}
        mock_conn.fetchrow.return_value = None  # Not found

        with pytest.raises(ValueError, match="Employee 'NON-EXISTENT' not found"):
            await handle_project_assignment_query(
                mock_engine,
                {
                    "namespace_id": str(ns_id),
                    "project_id": "PRJ-999",
                    "assign_lead_id": "NON-EXISTENT",
                },
            )


# ---------------------------------------------------------------------------
# 5. A2A Field Tech Dispatch Flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_field_tech_dispatch_query() -> None:
    """Field Tech dispatch evaluates cert validity, workload, and location."""
    ns_id = uuid4()
    mock_engine = MagicMock()

    from unittest.mock import patch

    with (
        patch(
            "nce.vertical_modules.hr.a2a.do_match_skills",
            new_callable=AsyncMock,
        ) as mock_match,
        patch(
            "nce.vertical_modules.hr.a2a.do_capacity",
            new_callable=AsyncMock,
        ) as mock_cap,
        patch(
            "nce.vertical_modules.hr.a2a.do_cert_status",
            new_callable=AsyncMock,
        ) as mock_certs,
    ):
        mock_match.return_value = {
            "candidates": [
                {
                    "employee_id": "TECH-1",
                    "name": "Tech One",
                    "role": "Technician",
                    "location_id": "OSLO-01",
                    "skills_matched": ["DSP programming"],
                    "certs_matched": ["Q-SYS Level 1"],
                },
                {
                    "employee_id": "TECH-2",
                    "name": "Tech Two",
                    "role": "Technician",
                    "location_id": "BERGEN-01",
                    "skills_matched": ["DSP programming"],
                    "certs_matched": ["Q-SYS Level 1"],
                },
            ]
        }

        # TECH-1 is 40% loaded with valid certs
        # TECH-2 is 95% loaded with expired cert
        def fake_cap(engine, params):
            emp = params["employee_id"]
            return {"utilization_pct": 40.0 if emp == "TECH-1" else 95.0}

        def fake_certs(engine, params):
            emp = params["employee_id"]
            if emp == "TECH-1":
                return {"valid_certs": ["Q-SYS Level 1"], "expiring_certs": [], "expired_certs": []}
            return {"valid_certs": [], "expiring_certs": [], "expired_certs": ["Q-SYS Level 1"]}

        mock_cap.side_effect = fake_cap
        mock_certs.side_effect = fake_certs

        res = await handle_field_tech_dispatch_query(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "work_order_id": "WO-2026-001",
                "required_skills": ["DSP programming"],
                "required_certs": ["Q-SYS Level 1"],
                "location_id": "OSLO-01",
            },
        )

        assert res["candidate_count"] == 2
        tech1 = next(c for c in res["candidates"] if c["employee_id"] == "TECH-1")
        assert tech1["dispatch_eligible"] is True
        assert tech1["location_matched"] is True
        assert tech1["has_expired_certs"] is False

        tech2 = next(c for c in res["candidates"] if c["employee_id"] == "TECH-2")
        assert tech2["dispatch_eligible"] is False
        assert tech2["location_matched"] is False
        assert tech2["has_expired_certs"] is True


# ---------------------------------------------------------------------------
# 6. A2A Vendor Contractor Skill Alignment
# ---------------------------------------------------------------------------


def test_handle_vendor_contractor_skill_align() -> None:
    """Contractor alignment maps taxonomy skills and resolves implied competencies."""
    engine = MagicMock()
    params = {
        "namespace_id": str(uuid4()),
        "contractor_id": "CONT-BETA-77",
        "skills": [
            {"name": "DSP programming", "level": 3},
            {"name": "fiber optic fusion splicing", "level": 4},
            "unrecognized_custom_skill",
        ],
        "certifications": [
            {"name": "CTS"},
            "Crestron DM-NVX",
            "Unknown Cert XYZ",
        ],
    }

    res = handle_vendor_contractor_skill_align(engine, params)
    assert res["contractor_id"] == "CONT-BETA-77"
    assert res["alignment_status"] == "partially_aligned"

    aligned_skills = {s["skill"] for s in res["aligned_skills"]}
    assert "DSP programming" in aligned_skills
    assert "Fiber optic fusion splicing" in aligned_skills

    assert "unrecognized_custom_skill" in res["unaligned_skills"]

    aligned_certs = {c["name"] for c in res["aligned_certifications"]}
    assert "CTS" in aligned_certs
    assert "Crestron DM-NVX" in aligned_certs
    assert "Unknown Cert XYZ" in res["unaligned_certifications"]

    # Implied skills from CTS and Crestron DM-NVX
    implied = res["implied_skills_from_certs"]
    assert "Site commissioning" in implied
    assert "AV-over-IP streaming" in implied


# ---------------------------------------------------------------------------
# 7. Morning Brief HR Slice (RL-1 & RL-2 Compliant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_morning_brief_hr_slice() -> None:
    """Morning Brief returns aggregated operational risks without person ranking."""
    ns_id = uuid4()
    mock_engine = MagicMock()
    mock_conn = AsyncMock()

    class FakeSession:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, *args):
            pass

    # Mock certs query: 3 expiring certs
    mock_conn.fetch.side_effect = [
        # cert_rows
        [
            {"authority": "InfoComm", "name": "CTS", "count": 2},
            {"authority": "QSC", "name": "Q-SYS Level 1", "count": 1},
        ],
        # compliance_rows: 1 absence triggering 4w plan
        [
            {
                "id": uuid4(),
                "employee_id": "EMP-010",
                "start_date": date.today() - timedelta(days=25),
                "raw": {"compliance_completed_milestones": []},
            }
        ],
        # dept_rows: Field Engineering (headcount 2)
        [
            {"department": "Field Engineering", "headcount": 2},
        ],
    ]
    # wo_load_row: 5 active work orders (utilization 100%)
    mock_conn.fetchrow.return_value = {"assigned_wos": 5}

    from unittest.mock import patch

    with patch("nce.vertical_modules.hr.a2a.scoped_pg_session", return_value=FakeSession()):
        slice_data = await get_morning_brief_hr_slice(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "cert_warn_days": 60,
                "capacity_threshold_pct": 80.0,
            },
        )

    assert slice_data["module"] == "hr"
    op_risk = slice_data["operational_risk"]
    assert op_risk["expiring_certifications_total"] == 3
    assert op_risk["open_statutory_deadlines_total"] == 1
    assert len(op_risk["teams_at_high_capacity"]) == 1
    assert op_risk["teams_at_high_capacity"][0]["department"] == "Field Engineering"

    # Prove RL-1 compliance: no person ranking or individual scores
    assert "leaderboard" not in slice_data
    assert "rankings" not in slice_data
    assert "top_performers" not in slice_data


# ---------------------------------------------------------------------------
# 8. RL-1 Ranking Refusal Guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_entry_points_refuse_ranking() -> None:
    """Every A2A function strictly refuses ranking / peer comparisons."""
    engine = MagicMock()
    ns_id = str(uuid4())

    # 1. handle_project_assignment_query
    with pytest.raises(HrRankingProhibitedError):
        await handle_project_assignment_query(
            engine,
            {"namespace_id": ns_id, "project_id": "PRJ-1", "sort_by": "score"},
        )

    # 2. handle_field_tech_dispatch_query
    with pytest.raises(HrRankingProhibitedError):
        await handle_field_tech_dispatch_query(
            engine,
            {"namespace_id": ns_id, "rank_against_peers": True},
        )

    # 3. handle_vendor_contractor_skill_align
    with pytest.raises(HrRankingProhibitedError):
        handle_vendor_contractor_skill_align(
            engine,
            {"namespace_id": ns_id, "contractor_id": "C-1", "top_performers": True},
        )

    # 4. get_morning_brief_hr_slice
    with pytest.raises(HrRankingProhibitedError):
        await get_morning_brief_hr_slice(
            engine,
            {"namespace_id": ns_id, "leaderboard": True},
        )
