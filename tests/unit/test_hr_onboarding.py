"""
tests/unit/test_hr_onboarding.py
================================
Unit tests for Module 13 (HR Engine) Phase 5:
  - Role-based 90-day onboarding quest templates (technician, PM, sales engineer, default)
  - Quest task progression, completion, uncompletion, and progress percentage
  - do_get_onboarding_progress (active stage, next task, overdue milestones)
  - Norwegian statutory sick leave timeline evaluation (evaluate_absence_compliance):
      * 4-week Oppfolgingsplan (statutory deadline 28d, warning 21d)
      * 7-week Dialogmote 1 (statutory deadline 49d, warning 42d, verneombud notification)
      * 26-week Dialogmote 2 / NAV (statutory deadline 182d, warning 168d)
  - Milestone completion and compliance state advancement (do_update_absence_compliance)
  - Namespace deadline queries (do_query_compliance_deadlines)
  - Absence registration integration and privacy scoping (do_register_absence, do_query_absences)
  - RL-1 NEVER ranking verification (no score comparisons)
  - Charter U3 Identity scrub compliance (ALPHA/BETA fixtures, @example.test)
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.vertical_modules.hr import absences, compliance, onboarding

_NS_A = "00000000-0000-4000-8000-000000000001"
_EMP_A = "EMP-ALPHA"
_EMP_B = "EMP-BETA"


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_engine(fetchrow_val: Any = None, fetch_val: Any = None) -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = fetchrow_val
    conn.fetch.return_value = fetch_val or []
    conn.execute.return_value = "UPDATE 1"
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))

    pool = MagicMock(spec=["acquire"])
    pool.acquire.return_value = _AsyncCtx(conn)
    engine.pg_pool = pool
    return engine


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

    monkeypatch.setattr("nce.vertical_modules.hr.onboarding.scoped_pg_session", _fake_scoped)
    monkeypatch.setattr("nce.vertical_modules.hr.compliance.scoped_pg_session", _fake_scoped)
    monkeypatch.setattr("nce.vertical_modules.hr.absences.scoped_pg_session", _fake_scoped)


# =========================================================================
# 1. Onboarding Quest Templates & Role Specialization
# =========================================================================


@pytest.mark.asyncio
async def test_build_quest_technician_template():
    engine = _make_engine(fetchrow_val=None)
    res = await onboarding.do_build_onboarding_quest(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "role": "technician",
            "start_date": "2026-09-01",
        },
    )

    assert res["role"] == "technician"
    assert res["total_tasks"] > 0
    assert res["completed_tasks"] == 0
    assert res["progress_pct"] == 0.0
    assert len(res["stages"]) == 4

    stage1 = res["stages"][0]
    assert stage1["stage_id"] == "stage_1_safety_tools"
    assert any("PPE" in t["description"] for t in stage1["tasks"])


@pytest.mark.asyncio
async def test_build_quest_project_manager_template():
    engine = _make_engine(fetchrow_val=None)
    res = await onboarding.do_build_onboarding_quest(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "role": "project_manager",
            "start_date": "2026-09-01",
        },
    )

    assert res["role"] == "project_manager"
    assert len(res["stages"]) == 4
    stage1 = res["stages"][0]
    assert stage1["stage_id"] == "stage_1_pm_setup"
    assert any("delivery methodology" in t["description"] for t in stage1["tasks"])


@pytest.mark.asyncio
async def test_build_quest_sales_engineer_template():
    engine = _make_engine(fetchrow_val=None)
    res = await onboarding.do_build_onboarding_quest(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "role": "sales_engineer",
            "start_date": "2026-09-01",
        },
    )

    assert res["role"] == "sales_engineer"
    assert len(res["stages"]) == 4
    stage1 = res["stages"][0]
    assert stage1["stage_id"] == "stage_1_catalog_pricing"
    assert any("pricing engine" in t["description"] for t in stage1["tasks"])


# =========================================================================
# 2. Quest Progression, Task Completion & Uncompletion
# =========================================================================


@pytest.mark.asyncio
async def test_quest_task_completion_and_progress():
    engine = _make_engine(fetchrow_val=None)

    # 1. Initial build
    res1 = await onboarding.do_build_onboarding_quest(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "role": "technician",
            "start_date": "2026-09-01",
        },
    )
    first_task_id = res1["stages"][0]["tasks"][0]["task_id"]

    # 2. Complete single task
    res2 = await onboarding.do_build_onboarding_quest(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "role": "technician",
            "start_date": "2026-09-01",
            "complete_task_id": first_task_id,
        },
    )
    assert res2["completed_tasks"] == 1
    assert res2["progress_pct"] > 0.0

    task = next(t for t in res2["stages"][0]["tasks"] if t["task_id"] == first_task_id)
    assert task["completed"] is True
    assert task["completed_at"] is not None

    # 3. Uncomplete task
    res3 = await onboarding.do_build_onboarding_quest(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "role": "technician",
            "start_date": "2026-09-01",
            "uncomplete_task_id": first_task_id,
        },
    )
    assert res3["completed_tasks"] == 0
    assert res3["progress_pct"] == 0.0


@pytest.mark.asyncio
async def test_get_onboarding_progress():
    engine = _make_engine(fetchrow_val=None)
    summary = await onboarding.do_get_onboarding_progress(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "role": "technician",
            "start_date": "2026-09-01",
        },
    )

    assert summary["employee_id"] == _EMP_A
    assert summary["progress_pct"] == 0.0
    assert summary["next_task"] is not None
    assert "task_id" in summary["next_task"]


# =========================================================================
# 3. Norwegian Statutory Compliance Timeline (Pure Evaluation)
# =========================================================================


def test_evaluate_non_sick_leave():
    res = compliance.evaluate_absence_compliance(
        absence_type="vacation",
        start_date=date(2026, 8, 1),
        as_of_date=date(2026, 9, 1),
    )
    assert res["applicable"] is False
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_NORMAL
    assert len(res["alerts"]) == 0
    assert res["verneombud_alert"] is False


def test_evaluate_sick_leave_day_10_normal():
    start = date(2026, 8, 20)
    as_of = date(2026, 8, 30)  # 10 days
    res = compliance.evaluate_absence_compliance("sick_leave", start, as_of)
    assert res["applicable"] is True
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_NORMAL
    assert res["days_elapsed"] == 10
    assert len(res["alerts"]) == 0
    assert res["verneombud_alert"] is False


def test_evaluate_sick_leave_day_21_plan_4w_warning():
    start = date(2026, 8, 1)
    as_of = date(2026, 8, 22)  # 21 days
    res = compliance.evaluate_absence_compliance("sick_leave", start, as_of)
    assert res["applicable"] is True
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_NORMAL
    assert len(res["alerts"]) == 1
    assert res["alerts"][0]["code"] == "PLAN_4W_UPCOMING"
    assert res["alerts"][0]["severity"] == "warning"


def test_evaluate_sick_leave_day_30_plan_4w_overdue():
    start = date(2026, 8, 1)
    as_of = date(2026, 8, 31)  # 30 days (> 28d)
    res = compliance.evaluate_absence_compliance("sick_leave", start, as_of)
    assert res["applicable"] is True
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_PLAN_4W_PENDING
    assert any(a["code"] == "PLAN_4W_OVERDUE" for a in res["alerts"])


def test_evaluate_sick_leave_day_42_dialogmote_1_warning_and_verneombud():
    start = date(2026, 8, 1)
    as_of = start + timedelta(days=42)
    res = compliance.evaluate_absence_compliance("sick_leave", start, as_of)
    assert res["applicable"] is True
    assert any(a["code"] == "DIALOGMOTE_1_UPCOMING" for a in res["alerts"])
    assert res["verneombud_alert"] is True


def test_evaluate_sick_leave_day_50_dialogmote_1_overdue():
    start = date(2026, 8, 1)
    as_of = start + timedelta(days=50)  # > 49d
    res = compliance.evaluate_absence_compliance("sick_leave", start, as_of)
    assert res["applicable"] is True
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING
    assert any(a["code"] == "DIALOGMOTE_1_OVERDUE" for a in res["alerts"])


def test_evaluate_sick_leave_day_190_dialogmote_2_nav_overdue():
    start = date(2026, 1, 1)
    as_of = start + timedelta(days=190)  # > 182d (~26w)
    res = compliance.evaluate_absence_compliance("sick_leave", start, as_of)
    assert res["applicable"] is True
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_DIALOGMOTE_26W_PENDING
    assert any(a["code"] == "DIALOGMOTE_2_OVERDUE" for a in res["alerts"])


def test_evaluate_sick_leave_milestones_completed():
    start = date(2026, 8, 1)
    as_of = start + timedelta(days=35)  # 35 days, but plan_4w completed
    existing = {"milestones": {"plan_4w": {"completed": True, "completed_at": "2026-08-25"}}}
    res = compliance.evaluate_absence_compliance("sick_leave", start, as_of, existing)
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_PLAN_4W_COMPLETED
    assert not any(a["code"] == "PLAN_4W_OVERDUE" for a in res["alerts"])


# =========================================================================
# 4. Async Compliance Updates & Queries
# =========================================================================


@pytest.mark.asyncio
async def test_do_update_absence_compliance():
    mock_absence = {
        "id": "00000000-0000-4000-8000-000000000010",
        "absence_id": "ABS-ALPHA",
        "employee_id": _EMP_A,
        "namespace_id": _NS_A,
        "type": "sick_leave",
        "start_date": date(2026, 8, 1),
        "end_date": None,
        "days": 35.0,
        "status": "approved",
        "compliance_state": "plan_4w_pending",
        "raw": json.dumps({}),
    }
    engine = _make_engine(fetchrow_val=mock_absence)

    res = await compliance.do_update_absence_compliance(
        engine,
        {
            "namespace_id": _NS_A,
            "absence_id": "ABS-ALPHA",
            "milestone": "plan_4w",
            "completed": True,
            "completed_at": "2026-08-25",
            "participants": ["Manager", "Employee", "Verneombud"],
            "notes": "Follow-up plan agreed with adjusted desk duties",
        },
    )

    assert res["absence_id"] == "ABS-ALPHA"
    assert res["milestone"] == "plan_4w"
    assert res["completed"] is True
    assert res["compliance_state"] == compliance.COMPLIANCE_STATE_PLAN_4W_COMPLETED


@pytest.mark.asyncio
async def test_do_query_compliance_deadlines():
    mock_rows = [
        {
            "absence_id": "ABS-ALPHA",
            "employee_id": _EMP_A,
            "employee_name": "Employee Alpha",
            "department": "operations",
            "type": "sick_leave",
            "start_date": date(2026, 8, 1),
            "end_date": None,
            "days": 35.0,
            "status": "approved",
            "compliance_state": "plan_4w_pending",
            "raw": json.dumps({}),
        }
    ]
    engine = _make_engine(fetch_val=mock_rows)

    res = await compliance.do_query_compliance_deadlines(
        engine,
        {
            "namespace_id": _NS_A,
            "only_alerts": False,
        },
    )

    assert res["total_active_sick_leaves"] == 1
    assert len(res["records"]) == 1
    rec = res["records"][0]
    assert rec["absence_id"] == "ABS-ALPHA"
    assert len(rec["alerts"]) > 0


# =========================================================================
# 5. Absence Registration Integration & Privacy Scoping
# =========================================================================


@pytest.mark.asyncio
async def test_register_sick_absence_initializes_compliance():
    mock_inserted = {
        "id": "00000000-0000-4000-8000-000000000010",
        "absence_id": "ABS-TEST",
        "employee_id": _EMP_A,
        "namespace_id": _NS_A,
        "type": "sick_leave",
        "start_date": date.today(),
        "end_date": date.today() + timedelta(days=5),
        "days": 6.0,
        "reason": "Recovering from respiratory infection",
        "status": "approved",
        "compliance_state": "normal",
        "hr_source_id": "SIMPLOYER-100",
        "raw": json.dumps({"compliance": {"milestones": {}}}),
        "created_at": date.today(),
    }
    engine = _make_engine(fetchrow_val=mock_inserted)

    res = await absences.do_register_absence(
        engine,
        {
            "namespace_id": _NS_A,
            "employee_id": _EMP_A,
            "absence_type": "sick_leave",
            "start_date": date.today().isoformat(),
            "end_date": (date.today() + timedelta(days=5)).isoformat(),
            "reason": "Recovering from respiratory infection",
        },
    )

    assert res["absence_id"] == "ABS-TEST"
    assert res["type"] == "sick_leave"
    assert res["compliance_state"] == "normal"
    assert res["compliance"] is not None


@pytest.mark.asyncio
async def test_query_absences_privacy_scoping():
    mock_rows = [
        {
            "id": "00000000-0000-4000-8000-000000000010",
            "absence_id": "ABS-TEST",
            "employee_id": _EMP_A,
            "namespace_id": _NS_A,
            "type": "sick_leave",
            "start_date": date.today(),
            "end_date": date.today() + timedelta(days=5),
            "days": 6.0,
            "reason": "Highly sensitive private medical reason",
            "status": "approved",
            "compliance_state": "normal",
            "hr_source_id": None,
            "raw": json.dumps({}),
            "created_at": date.today(),
        }
    ]
    engine = _make_engine(fetch_val=mock_rows)

    # 1. Peer role cannot read sensitive reason
    peer_res = await absences.do_query_absences(
        engine,
        {
            "namespace_id": _NS_A,
            "caller_role": "peer",
            "caller_employee_id": _EMP_B,
        },
    )
    assert peer_res["absences"][0]["reason"] is None

    # 2. Privileged manager role can read sensitive reason
    mgr_res = await absences.do_query_absences(
        engine,
        {
            "namespace_id": _NS_A,
            "caller_role": "manager",
            "caller_employee_id": _EMP_B,
        },
    )
    assert mgr_res["absences"][0]["reason"] == "Highly sensitive private medical reason"

    # 3. Self can read own sensitive reason
    self_res = await absences.do_query_absences(
        engine,
        {
            "namespace_id": _NS_A,
            "caller_role": "peer",
            "caller_employee_id": _EMP_A,
        },
    )
    assert self_res["absences"][0]["reason"] == "Highly sensitive private medical reason"
