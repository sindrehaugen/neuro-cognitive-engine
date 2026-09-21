"""
tests/integration/test_hr_compliance_dashboard_milestones_live.py
=====================================================================
Live-Postgres sibling for `get_morning_brief_hr_slice`'s statutory
compliance deadline tracking (`nce/vertical_modules/hr/a2a.py`).
Dispatched from E's fourteen-site jsonb-decode sweep, assigned to Lane F.

THE BUG, EXACTLY
------------------
`absences.raw` is jsonb; no jsonb codec is registered on this project's
pool (confirmed: nce/semantic_search.py's own comment states the same
fact), so asyncpg always hands the column back as a raw JSON string,
never a dict. The pre-fix code was
`raw_data = row["raw"] if isinstance(row["raw"], dict) else {}` --
against a real row this is unconditionally False, so
`raw_data.get("compliance_completed_milestones")` was always None, and
`completed` was always an empty set.

THE CONSEQUENCE -- was meant to be a silent-wrong-answer, turned out to
be a crash, until a SECOND bug in the same loop was also fixed
--------------------------------------------------------------------------
This dashboard tracks Norwegian sick-leave follow-up deadlines
(oppfolgingsplan at 4 weeks, dialogmote at 7 and 26 weeks). The intended
consequence of the jsonb bug alone: EVERY open sick leave past a given
threshold counted as having that milestone still PENDING, even when
`raw.compliance_completed_milestones` genuinely recorded it as done --
pending-milestone counts that could only ever be too high, never correct.

Found live while building this test, not reasoned about: `absences.
start_date` is `timestamp with time zone`, so asyncpg always hands it
back as a real `datetime`, while the function computes `today =
date.today()` and does `(today - s_date).days` two lines after the jsonb
read -- `date - datetime` raises `TypeError` unconditionally whenever a
row exists. This function could not have returned a result at all for
any namespace with an open sick leave until BOTH bugs were fixed
together; the jsonb fix alone would have been unreachable in production,
since the crash happens on the very next line for every row regardless.
Both fixed here, since fixing one without the other leaves this test
unable to prove either.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import asyncpg
import pytest

from nce.orchestrator import NCEEngine
from nce.vertical_modules.hr.a2a import get_morning_brief_hr_slice
from nce.vertical_modules.hr.compliance import (
    COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING,
    COMPLIANCE_STATE_PLAN_4W_PENDING,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


async def _create_employee(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, employee_id: str
) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO employees (employee_id, namespace_id, name) VALUES ($1, $2, $3)",
            employee_id,
            namespace_id,
            "Compliance Test Employee",
        )


async def _create_sick_leave(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    employee_id: str,
    absence_id: str,
    days_ago: int,
    raw: dict,
) -> None:
    start = date.today() - timedelta(days=days_ago)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO absences
                (absence_id, employee_id, namespace_id, type, start_date, days, status, raw)
            VALUES ($1, $2, $3, 'sick_leave', $4, 1, 'approved', $5::jsonb)
            """,
            absence_id,
            employee_id,
            namespace_id,
            start,
            json.dumps(raw),
        )


@pytest.mark.asyncio
async def test_completed_4w_milestone_is_not_counted_as_pending(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """The regression: a sick leave 30 days old (past the 21-day 4-week
    threshold) whose raw genuinely records the 4-week plan as completed
    must NOT be counted as having that milestone pending.
    """
    employee_id = f"EMP-COMPLIANCE-{uuid.uuid4().hex[:8]}"
    await _create_employee(pg_pool, namespace_id, employee_id)
    await _create_sick_leave(
        pg_pool,
        namespace_id,
        employee_id,
        f"ABS-{uuid.uuid4().hex[:8]}",
        days_ago=30,
        raw={"compliance_completed_milestones": ["oppfolgingsplan_4w"]},
    )

    result = await get_morning_brief_hr_slice(engine, {"namespace_id": str(namespace_id)})

    milestone_counts = result["operational_risk"]["statutory_deadlines_pending"]
    assert milestone_counts[COMPLIANCE_STATE_PLAN_4W_PENDING] == 0, (
        f"a genuinely completed 4-week plan milestone was still counted as pending: "
        f"{milestone_counts} -- the exact silent-over-report this test exists to catch"
    )


@pytest.mark.asyncio
async def test_uncompleted_milestone_is_still_correctly_counted_as_pending(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """Must not break: a sick leave past the threshold whose raw does NOT
    record the milestone as completed must still be counted as pending --
    otherwise a fix that always returns "nothing pending" would pass the
    test above for the wrong reason.
    """
    employee_id = f"EMP-COMPLIANCE-{uuid.uuid4().hex[:8]}"
    await _create_employee(pg_pool, namespace_id, employee_id)
    await _create_sick_leave(
        pg_pool,
        namespace_id,
        employee_id,
        f"ABS-{uuid.uuid4().hex[:8]}",
        days_ago=30,
        raw={},
    )

    result = await get_morning_brief_hr_slice(engine, {"namespace_id": str(namespace_id)})

    milestone_counts = result["operational_risk"]["statutory_deadlines_pending"]
    assert milestone_counts[COMPLIANCE_STATE_PLAN_4W_PENDING] == 1, (
        "a genuinely incomplete 4-week plan milestone was not counted as pending -- "
        "a fix that stops reading raw at all would pass the completed-case test above "
        "for the wrong reason; this positive case must still work"
    )


@pytest.mark.asyncio
async def test_completed_7w_dialogmote_is_not_counted_as_pending(
    engine: NCEEngine, namespace_id: uuid.UUID, pg_pool: asyncpg.Pool
) -> None:
    """A second, independent milestone (7-week dialogmote, 42-day
    threshold) with both milestones marked completed in raw -- proves the
    fix reads the full completed set, not just the first-checked one.
    """
    employee_id = f"EMP-COMPLIANCE-{uuid.uuid4().hex[:8]}"
    await _create_employee(pg_pool, namespace_id, employee_id)
    await _create_sick_leave(
        pg_pool,
        namespace_id,
        employee_id,
        f"ABS-{uuid.uuid4().hex[:8]}",
        days_ago=50,
        raw={"compliance_completed_milestones": ["oppfolgingsplan_4w", "dialogmote_1_7w"]},
    )

    result = await get_morning_brief_hr_slice(engine, {"namespace_id": str(namespace_id)})

    milestone_counts = result["operational_risk"]["statutory_deadlines_pending"]
    assert milestone_counts[COMPLIANCE_STATE_PLAN_4W_PENDING] == 0
    assert milestone_counts[COMPLIANCE_STATE_DIALOGMOTE_7W_PENDING] == 0, (
        f"a genuinely completed 7-week dialogmote milestone was still counted as "
        f"pending: {milestone_counts}"
    )
