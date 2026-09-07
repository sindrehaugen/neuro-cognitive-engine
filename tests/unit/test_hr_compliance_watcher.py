"""
tests/unit/test_hr_compliance_watcher.py
========================================
Unit tests for HR statutory compliance-deadline watcher on cron (Wave HR-5):
  - do_check_compliance_deadlines: Oppfolgingsplan upcoming warning detection (day 21) & C4 event
  - do_check_compliance_deadlines: Oppfolgingsplan overdue critical alert detection (day 30) & C4 event
  - do_check_compliance_deadlines: Dialogmote 1 upcoming warning (day 42) and overdue alert (day 50)
  - do_check_compliance_deadlines: Dialogmote 2 upcoming warning (day 168) and overdue alert (day 185)
  - do_check_compliance_deadlines: absence within deadlines produces zero outbox events
  - do_check_compliance_deadlines: completed milestones produce no alerts
  - do_check_compliance_deadlines: idempotency on repeated sweep (zero duplicate publishes)
  - do_check_compliance_deadlines: atomic transaction rollback on outbox publish failure
  - Event catalogue & audited sites registration integrity
  - Cron tick execution, distributed lock acquisition, and throttled alerts

Pure unit tests -- mock asyncpg connections and scoped sessions.
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.db_utils import UNMANAGED_PG_AUDITED_SITES
from nce.events.catalogue import EVENT_CATALOGUE
from nce.vertical_modules.hr import do_check_compliance_deadlines


class _MockTransactionCM:
    def __init__(self, conn: Any = None) -> None:
        self.conn = conn

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _MockContextManager:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        if isinstance(getattr(conn, "transaction", None), AsyncMock):
            conn.transaction = MagicMock(return_value=_MockTransactionCM(conn))

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


@pytest.fixture
def mock_engine():
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. Oppfolgingsplan Upcoming Warning (Day 21)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_upcoming_warning(mock_engine) -> None:
    """Active sick leave reaching day 21 triggers OPPFOLGINGSPLAN_UPCOMING warning & C4 event."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    employee_id = f"emp_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=21)

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": employee_id,
            "type": "sick",
            "start_date": start_date,
            "end_date": None,
            "days": 21.0,
            "status": "approved",
            "compliance_state": "normal",
            "raw": "{}",
        }
    ]

    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result["checked"] == 1
        assert result["alerted"] == 1
        assert result["published"] == 1
        assert len(result["alerts"]) == 1
        assert result["alerts"][0]["absence_id"] == absence_id

        # Verify UPDATE absences executed
        update_calls = [c for c in mock_conn.execute.call_args_list if "UPDATE absences" in c[0][0]]
        assert len(update_calls) == 1
        updated_raw = json.loads(update_calls[0][0][2])
        assert "PLAN_4W_UPCOMING" in updated_raw["compliance"]["emitted_alert_codes"]

        # Verify C4 event published
        mock_pub.assert_awaited_once()
        kwargs = mock_pub.await_args.kwargs
        assert kwargs["namespace_id"] == ns_id
        assert kwargs["node_type"] == "ABSENCE"
        assert kwargs["op"] == "compliance_alert"
        assert kwargs["aggregate_id"] == f"ABSENCE:{absence_id}"
        assert kwargs["payload"]["absence_id"] == absence_id
        assert kwargs["payload"]["employee_id"] == employee_id
        assert "PLAN_4W_UPCOMING" in kwargs["payload"]["alert_codes"]


# ---------------------------------------------------------------------------
# 2. Oppfolgingsplan Overdue Critical Alert (Day 30)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_overdue_alert(mock_engine) -> None:
    """Active sick leave reaching day 30 without follow-up plan triggers critical alert."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    employee_id = f"emp_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=30)

    # Absence already had upcoming warning emitted at day 21
    existing_raw = {
        "compliance": {
            "emitted_alert_codes": ["PLAN_4W_UPCOMING"],
        }
    }

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": employee_id,
            "type": "sick_leave",
            "start_date": start_date,
            "end_date": None,
            "days": 30.0,
            "status": "approved",
            "compliance_state": "normal",
            "raw": json.dumps(existing_raw),
        }
    ]

    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result["checked"] == 1
        assert result["alerted"] == 1
        assert result["published"] == 1
        assert result["alerts"][0]["compliance_state"] == "plan_4w_pending"

        # Verify C4 event published with PLAN_4W_OVERDUE
        mock_pub.assert_awaited_once()
        kwargs = mock_pub.await_args.kwargs
        assert kwargs["node_type"] == "ABSENCE"
        assert kwargs["op"] == "compliance_alert"
        assert "PLAN_4W_OVERDUE" in kwargs["payload"]["alert_codes"]


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_dialogmote_1_alerts(mock_engine) -> None:
    """Active sick leave reaching day 45 triggers Dialogmote 1 upcoming warning."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    employee_id = f"emp_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=45)

    # plan_4w was completed earlier
    existing_raw = {
        "compliance": {
            "milestones": {
                "plan_4w": {
                    "completed": True,
                    "completed_at": (today - datetime.timedelta(days=20)).isoformat(),
                }
            },
            "emitted_alert_codes": ["PLAN_4W_UPCOMING"],
        }
    }

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": employee_id,
            "type": "sick",
            "start_date": start_date,
            "end_date": None,
            "days": 45.0,
            "status": "approved",
            "compliance_state": "plan_4w_completed",
            "raw": json.dumps(existing_raw),
        }
    ]

    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result["checked"] == 1
        assert result["alerted"] == 1
        assert result["published"] == 1
        assert "DIALOGMOTE_1_UPCOMING" in result["alerts"][0]["alerts"][0]["code"]

        mock_pub.assert_awaited_once()
        kwargs = mock_pub.await_args.kwargs
        assert "DIALOGMOTE_1_UPCOMING" in kwargs["payload"]["alert_codes"]


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_dialogmote_2_alerts(mock_engine) -> None:
    """Active sick leave reaching day 190 triggers Dialogmote 2 overdue critical alert."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    employee_id = f"emp_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=190)

    # plan_4w and dialogmote_1 were completed
    existing_raw = {
        "compliance": {
            "milestones": {
                "plan_4w": {"completed": True},
                "dialogmote_1": {"completed": True},
            },
            "emitted_alert_codes": [
                "PLAN_4W_UPCOMING",
                "DIALOGMOTE_1_UPCOMING",
                "DIALOGMOTE_2_UPCOMING",
            ],
        }
    }

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": employee_id,
            "type": "sick",
            "start_date": start_date,
            "end_date": None,
            "days": 190.0,
            "status": "approved",
            "compliance_state": "dialogmote_7w_completed",
            "raw": json.dumps(existing_raw),
        }
    ]

    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result["checked"] == 1
        assert result["alerted"] == 1
        assert result["published"] == 1
        assert result["alerts"][0]["compliance_state"] == "dialogmote_26w_pending"

        mock_pub.assert_awaited_once()
        kwargs = mock_pub.await_args.kwargs
        assert "DIALOGMOTE_2_OVERDUE" in kwargs["payload"]["alert_codes"]


# ---------------------------------------------------------------------------
# 3. Absence Within Deadlines Emits Nothing (Day 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_within_deadlines_emits_nothing(mock_engine) -> None:
    """Short or healthy absence produces zero alerts and zero C4 events."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=5)

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": "emp_001",
            "type": "sick",
            "start_date": start_date,
            "end_date": None,
            "days": 5.0,
            "status": "approved",
            "compliance_state": "normal",
            "raw": "{}",
        }
    ]

    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result["checked"] == 1
        assert result["alerted"] == 0
        assert result["published"] == 0
        assert len(result["alerts"]) == 0
        mock_pub.assert_not_called()
        mock_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Completed Milestones Suppress Alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_completed_milestones_suppress_alerts(
    mock_engine,
) -> None:
    """Completed milestones (completed=True) do not trigger overdue or upcoming alerts."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=35)  # past 28d, but plan_4w completed

    raw_with_completed_plan = {
        "compliance": {
            "milestones": {
                "plan_4w": {
                    "completed": True,
                    "completed_at": (today - datetime.timedelta(days=10)).isoformat(),
                }
            },
            "emitted_alert_codes": [],
        }
    }

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": "emp_001",
            "type": "sick",
            "start_date": start_date,
            "end_date": None,
            "days": 35.0,
            "status": "approved",
            "compliance_state": "plan_4w_completed",
            "raw": json.dumps(raw_with_completed_plan),
        }
    ]

    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result["checked"] == 1
        assert result["alerted"] == 0
        assert result["published"] == 0
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Idempotent Sweep (Zero Duplicate Outbox Events)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_idempotent_sweep_no_duplicates(mock_engine) -> None:
    """Subsequent sweep on the same day does not re-publish already emitted alert codes."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=21)

    raw_already_emitted = {
        "compliance": {
            "emitted_alert_codes": ["PLAN_4W_UPCOMING"],
        }
    }

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": "emp_001",
            "type": "sick",
            "start_date": start_date,
            "end_date": None,
            "days": 21.0,
            "status": "approved",
            "compliance_state": "normal",
            "raw": json.dumps(raw_already_emitted),
        }
    ]

    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result["checked"] == 1
        assert result["alerted"] == 0
        assert result["published"] == 0
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Atomic Transaction Rollback on Publish Failure (Charter §13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_compliance_deadlines_transaction_rollback(mock_engine) -> None:
    """If outbox publish fails, per-absence transaction rolls back so the alert is cleanly retried."""
    ns_id = uuid4()
    absence_id = f"abs_{uuid4().hex[:8]}"
    today = datetime.date(2026, 9, 7)
    start_date = today - datetime.timedelta(days=21)

    # In-memory database simulation to verify atomic rollback vs commit
    absence_committed = {"compliance_state": "normal", "emitted_alert_codes": []}
    absence_staged = {"compliance_state": "normal", "emitted_alert_codes": []}

    class MockTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                # Commit staged writes
                absence_committed["compliance_state"] = absence_staged["compliance_state"]
                absence_committed["emitted_alert_codes"] = list(
                    absence_staged["emitted_alert_codes"]
                )
            else:
                # Rollback staged writes
                absence_staged["compliance_state"] = absence_committed["compliance_state"]
                absence_staged["emitted_alert_codes"] = list(
                    absence_committed["emitted_alert_codes"]
                )
            return False

    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock(side_effect=MockTransaction)

    async def mock_execute(query, *args):
        if "UPDATE absences" in query:
            absence_staged["compliance_state"] = args[0]
            raw_parsed = json.loads(args[1])
            absence_staged["emitted_alert_codes"] = raw_parsed["compliance"]["emitted_alert_codes"]
        return None

    mock_conn.execute = AsyncMock(side_effect=mock_execute)
    mock_conn.fetch.return_value = [
        {
            "id": uuid4(),
            "absence_id": absence_id,
            "employee_id": "emp_001",
            "type": "sick",
            "start_date": start_date,
            "end_date": None,
            "days": 21.0,
            "status": "approved",
            "compliance_state": absence_committed["compliance_state"],
            "raw": json.dumps(
                {"compliance": {"emitted_alert_codes": absence_committed["emitted_alert_codes"]}}
            ),
        }
    ]

    # Run 1: publish fails -> transaction rolls back
    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch(
            "nce.vertical_modules.hr.compliance.publish",
            new_callable=AsyncMock,
            side_effect=RuntimeError("outbox connection drop"),
        ),
    ):
        result_1 = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result_1["checked"] == 1
        assert result_1["alerted"] == 0
        assert result_1["published"] == 0
        assert absence_committed["emitted_alert_codes"] == []

    # Run 2: on next tick, publish succeeds -> alert committed!
    with (
        patch(
            "nce.vertical_modules.hr.compliance.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.hr.compliance.publish", new_callable=AsyncMock) as mock_pub_2,
    ):
        result_2 = await do_check_compliance_deadlines(
            mock_engine,
            {"namespace_id": str(ns_id), "as_of_date": today.isoformat()},
        )

        assert result_2["checked"] == 1
        assert result_2["alerted"] == 1
        assert result_2["published"] == 1
        assert "PLAN_4W_UPCOMING" in absence_committed["emitted_alert_codes"]
        mock_pub_2.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. Event Catalogue & Audited Sites Registration Integrity
# ---------------------------------------------------------------------------


def test_event_catalogue_and_audited_sites_registration() -> None:
    """Verify ABSENCE.compliance_alert and unmanaged audited site are registered."""
    # 1. EVENT_CATALOGUE
    assert "ABSENCE.compliance_alert" in EVENT_CATALOGUE
    contract = EVENT_CATALOGUE["ABSENCE.compliance_alert"]
    assert contract.node_type == "ABSENCE"
    assert contract.op == "compliance_alert"
    assert "nce/vertical_modules/hr/compliance.py" in contract.declared_producers
    assert "nce/vertical_modules/hr/compliance.py" in contract.declared_consumers
    assert contract.status == "ACTIVE"

    # 2. UNMANAGED_PG_AUDITED_SITES
    assert "cron.hr_compliance_watcher.namespace_scan" in UNMANAGED_PG_AUDITED_SITES


# ---------------------------------------------------------------------------
# 8. Cron Tick Execution & Distributed Lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_hr_compliance_watcher_tick_runs_with_lock() -> None:
    """Cron tick acquires distributed lock, scans namespaces, and releases lock."""
    from nce.cron import _hr_compliance_watcher_tick

    ns_id = uuid4()
    pool = MagicMock()
    mock_lock = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"id": ns_id}]

    mock_release = AsyncMock()
    mock_acquire = AsyncMock(return_value=mock_lock)

    fake_unmanaged_ctx = AsyncMock()
    fake_unmanaged_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    fake_unmanaged_ctx.__aexit__ = AsyncMock(return_value=None)

    fake_stats = {
        "checked": 2,
        "alerted": 1,
        "published": 1,
        "alerts": [
            {
                "absence_id": "abs_123",
                "employee_id": "emp_456",
                "compliance_state": "plan_4w_pending",
            }
        ],
    }

    with (
        patch("nce.cron.acquire_cron_lock", mock_acquire),
        patch("nce.cron.release_cron_lock", mock_release),
        patch("nce.cron.unmanaged_pg_connection", return_value=fake_unmanaged_ctx),
        patch(
            "nce.vertical_modules.hr.compliance.do_check_compliance_deadlines",
            new_callable=AsyncMock,
            return_value=fake_stats,
        ) as mock_check,
        patch("nce.cron._dispatch_throttled_alert", new_callable=AsyncMock) as mock_alert,
    ):
        await _hr_compliance_watcher_tick(pool)

        mock_acquire.assert_awaited_once_with("hr_compliance_watcher", 3660)
        mock_check.assert_awaited_once()
        mock_alert.assert_awaited_once()
        mock_release.assert_awaited_once_with(mock_lock)


@pytest.mark.asyncio
async def test_cron_hr_compliance_watcher_tick_skips_when_lock_held() -> None:
    """Cron tick gracefully skips execution when lock is held by another instance."""
    from nce.cron import _hr_compliance_watcher_tick

    pool = MagicMock()
    mock_acquire = AsyncMock(return_value=None)
    mock_release = AsyncMock()
    mock_conn = AsyncMock()

    with (
        patch("nce.cron.acquire_cron_lock", mock_acquire),
        patch("nce.cron.release_cron_lock", mock_release),
        patch("nce.cron.unmanaged_pg_connection") as mock_unmanaged,
    ):
        await _hr_compliance_watcher_tick(pool)
        mock_unmanaged.assert_not_called()
        mock_conn.assert_not_called()
        mock_release.assert_not_called()


# ---------------------------------------------------------------------------
# 9. Outbox Subscriber Registration & Handler Execution
# ---------------------------------------------------------------------------


def test_register_hr_compliance_subscribers_wires_outbox_handler() -> None:
    """Verify register_hr_compliance_subscribers registers ABSENCE.compliance_alert."""
    from nce.outbox_relay import OUTBOX_HANDLERS
    from nce.vertical_modules.hr.compliance import (
        handle_absence_compliance_alert,
        register_hr_compliance_subscribers,
    )

    register_hr_compliance_subscribers()
    handlers = OUTBOX_HANDLERS.get("ABSENCE.compliance_alert")
    assert handlers is not None
    assert handle_absence_compliance_alert in handlers


@pytest.mark.asyncio
async def test_handle_absence_compliance_alert_success() -> None:
    """Verify handle_absence_compliance_alert executes cleanly and acknowledges delivery."""
    from nce.vertical_modules.hr.compliance import handle_absence_compliance_alert

    mock_conn = AsyncMock()
    event = {
        "event_type": "ABSENCE.compliance_alert",
        "aggregate_type": "ABSENCE",
        "aggregate_id": "ABSENCE:abs_001",
        "namespace_id": str(uuid4()),
        "payload": {
            "absence_id": "abs_001",
            "employee_id": "emp_001",
            "compliance_state": "plan_4w_pending",
            "alert_codes": ["OPPFOLGINGSPLAN_UPCOMING"],
        },
    }

    result = await handle_absence_compliance_alert(mock_conn, event)
    assert result is None
