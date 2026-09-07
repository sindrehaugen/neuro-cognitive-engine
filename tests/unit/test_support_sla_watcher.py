"""
tests/unit/test_support_sla_watcher.py
======================================
Unit tests for Support SLA breach watcher (Wave SU-2):
  - do_check_sla_breaches: first-response SLA breach detection and C4 publish
  - do_check_sla_breaches: resolution SLA breach detection and C4 publish
  - do_check_sla_breaches: breach escalation from first_response to both
  - do_check_sla_breaches: non-breached ticket produces zero outbox events
  - do_check_sla_breaches: paused intervals correctly extend deadlines
  - do_check_sla_breaches: idempotency on repeated sweep (zero duplicate publishes)
  - Event catalogue & audited sites registration integrity
  - Cron tick execution, distributed lock acquisition, and throttled alerts

Pure unit tests — mock asyncpg connections and scoped sessions.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.db_utils import UNMANAGED_PG_AUDITED_SITES
from nce.events.catalogue import EVENT_CATALOGUE
from nce.vertical_modules.support import do_check_sla_breaches


class _MockContextManager:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

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
# 1. First-Response SLA Breach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_sla_breaches_first_response_breach(mock_engine) -> None:
    """Ticket exceeding first-response deadline with no response triggers breach & C4 event."""
    ns_id = uuid4()
    ticket_id = uuid4()
    now = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    created_at = now - datetime.timedelta(hours=6)
    # Standard profile, medium priority: first_response_hours = 4.0, resolution_hours = 24.0
    first_resp_due = created_at + datetime.timedelta(hours=4)
    res_due = created_at + datetime.timedelta(hours=24)

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": ticket_id,
            "status": "open",
            "priority": "medium",
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "created_at": created_at,
            "first_response_due": first_resp_due,
            "resolution_due": res_due,
            "breached": False,
            "breach_type": None,
            "paused_intervals": [],
        }
    ]

    with (
        patch(
            "nce.vertical_modules.support.sla.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.support.sla.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_sla_breaches(
            mock_engine,
            {"namespace_id": str(ns_id), "now": now},
        )

        assert result["checked"] == 1
        assert result["breached"] == 1
        assert result["published"] == 1
        assert result["total_active_breaches"] == 1
        assert len(result["breaches"]) == 1
        assert result["breaches"][0]["breach_type"] == "first_response"
        assert result["breaches"][0]["newly_breached"] is True

        # Verify sla_clocks update executed
        update_calls = [
            c for c in mock_conn.execute.call_args_list if "UPDATE sla_clocks" in c[0][0]
        ]
        assert len(update_calls) == 1
        assert update_calls[0][0][3] == "first_response"

        # Verify C4 event published
        mock_pub.assert_awaited_once()
        kwargs = mock_pub.await_args.kwargs
        assert kwargs["namespace_id"] == ns_id
        assert kwargs["node_type"] == "TICKET"
        assert kwargs["op"] == "sla_breached"
        assert kwargs["aggregate_id"] == f"TICKET:{ticket_id}"
        assert kwargs["payload"]["breach_type"] == "first_response"
        assert kwargs["payload"]["ticket_id"] == str(ticket_id)


# ---------------------------------------------------------------------------
# 2. Resolution SLA Breach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_sla_breaches_resolution_breach(mock_engine) -> None:
    """Ticket with timely first response but past resolution deadline triggers breach & C4 event."""
    ns_id = uuid4()
    ticket_id = uuid4()
    now = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    created_at = now - datetime.timedelta(hours=30)
    first_resp_due = created_at + datetime.timedelta(hours=4)
    res_due = created_at + datetime.timedelta(hours=24)
    first_resp_at = created_at + datetime.timedelta(hours=1)  # responded in 1h (< 4h)

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": ticket_id,
            "status": "in_progress",
            "priority": "medium",
            "sla_profile": "standard",
            "first_response_at": first_resp_at,
            "resolved_at": None,
            "created_at": created_at,
            "first_response_due": first_resp_due,
            "resolution_due": res_due,
            "breached": False,
            "breach_type": None,
            "paused_intervals": [],
        }
    ]

    with (
        patch(
            "nce.vertical_modules.support.sla.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.support.sla.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_sla_breaches(
            mock_engine,
            {"namespace_id": str(ns_id), "now": now},
        )

        assert result["checked"] == 1
        assert result["breached"] == 1
        assert result["published"] == 1
        assert result["total_active_breaches"] == 1
        assert result["breaches"][0]["breach_type"] == "resolution"

        mock_pub.assert_awaited_once()
        kwargs = mock_pub.await_args.kwargs
        assert kwargs["node_type"] == "TICKET"
        assert kwargs["op"] == "sla_breached"
        assert kwargs["payload"]["breach_type"] == "resolution"


# ---------------------------------------------------------------------------
# 3. Breach Escalation: first_response -> both
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_sla_breaches_escalates_to_both(mock_engine) -> None:
    """Already first_response-breached ticket that subsequently breaches resolution escalates to 'both'."""
    ns_id = uuid4()
    ticket_id = uuid4()
    now = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    created_at = now - datetime.timedelta(hours=30)
    first_resp_due = created_at + datetime.timedelta(hours=4)
    res_due = created_at + datetime.timedelta(hours=24)

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": ticket_id,
            "status": "open",
            "priority": "medium",
            "sla_profile": "standard",
            "first_response_at": None,  # never responded
            "resolved_at": None,
            "created_at": created_at,
            "first_response_due": first_resp_due,
            "resolution_due": res_due,
            "breached": True,
            "breach_type": "first_response",  # previously breached for first_response only
            "paused_intervals": [],
        }
    ]

    with (
        patch(
            "nce.vertical_modules.support.sla.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.support.sla.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_sla_breaches(
            mock_engine,
            {"namespace_id": str(ns_id), "now": now},
        )

        assert result["checked"] == 1
        assert result["breached"] == 1  # newly escalated
        assert result["published"] == 1
        assert result["total_active_breaches"] == 1
        assert result["breaches"][0]["breach_type"] == "both"
        assert result["breaches"][0]["newly_breached"] is True

        mock_pub.assert_awaited_once()
        assert mock_pub.await_args.kwargs["payload"]["breach_type"] == "both"


# ---------------------------------------------------------------------------
# 4. Non-Breached Ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_sla_breaches_within_deadlines_emits_nothing(mock_engine) -> None:
    """Ticket created recently and well within deadlines produces zero publications."""
    ns_id = uuid4()
    ticket_id = uuid4()
    now = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    created_at = now - datetime.timedelta(minutes=15)
    first_resp_due = created_at + datetime.timedelta(hours=4)
    res_due = created_at + datetime.timedelta(hours=24)

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": ticket_id,
            "status": "open",
            "priority": "medium",
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "created_at": created_at,
            "first_response_due": first_resp_due,
            "resolution_due": res_due,
            "breached": False,
            "breach_type": None,
            "paused_intervals": [],
        }
    ]

    with (
        patch(
            "nce.vertical_modules.support.sla.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.support.sla.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_sla_breaches(
            mock_engine,
            {"namespace_id": str(ns_id), "now": now},
        )

        assert result["checked"] == 1
        assert result["breached"] == 0
        assert result["published"] == 0
        assert result["total_active_breaches"] == 0
        assert len(result["breaches"]) == 0
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Paused Intervals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_sla_breaches_paused_intervals_extend_deadline(mock_engine) -> None:
    """Paused intervals extend effective deadlines and prevent false breach detection."""
    ns_id = uuid4()
    ticket_id = uuid4()
    now = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    created_at = now - datetime.timedelta(hours=5)
    first_resp_due = created_at + datetime.timedelta(hours=4)
    res_due = created_at + datetime.timedelta(hours=24)
    # Ticket paused for 2 hours (7200 seconds) -> effective first_response_due is 6 hours from created_at
    paused_intervals = [{"duration_seconds": 7200}]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": ticket_id,
            "status": "waiting_customer",
            "priority": "medium",
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "created_at": created_at,
            "first_response_due": first_resp_due,
            "resolution_due": res_due,
            "breached": False,
            "breach_type": None,
            "paused_intervals": paused_intervals,
        }
    ]

    with (
        patch(
            "nce.vertical_modules.support.sla.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.support.sla.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_sla_breaches(
            mock_engine,
            {"namespace_id": str(ns_id), "now": now},
        )

        assert result["checked"] == 1
        assert result["breached"] == 0
        assert result["published"] == 0
        assert result["total_active_breaches"] == 0
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Idempotency: Repeated Sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_sla_breaches_idempotent_sweep_no_duplicate_publish(mock_engine) -> None:
    """Subsequent sweep on already-breached ticket does not emit duplicate events."""
    ns_id = uuid4()
    ticket_id = uuid4()
    now = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    created_at = now - datetime.timedelta(hours=30)
    first_resp_due = created_at + datetime.timedelta(hours=4)
    res_due = created_at + datetime.timedelta(hours=24)

    mock_conn = AsyncMock()
    # Ticket already marked breached for 'both'
    mock_conn.fetch.return_value = [
        {
            "id": ticket_id,
            "status": "open",
            "priority": "medium",
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "created_at": created_at,
            "first_response_due": first_resp_due,
            "resolution_due": res_due,
            "breached": True,
            "breach_type": "both",
            "paused_intervals": [],
        }
    ]

    with (
        patch(
            "nce.vertical_modules.support.sla.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.support.sla.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_sla_breaches(
            mock_engine,
            {"namespace_id": str(ns_id), "now": now},
        )

        assert result["checked"] == 1
        assert result["breached"] == 0  # No new breaches
        assert result["published"] == 0  # No new publications
        assert result["total_active_breaches"] == 1  # Still in breached state
        assert len(result["breaches"]) == 1
        assert result["breaches"][0]["newly_breached"] is False
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Auto-seeding missing sla_clocks rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_sla_breaches_seeds_missing_sla_clocks(mock_engine) -> None:
    """When a ticket has no sla_clocks row, do_check_sla_breaches initializes targets."""
    ns_id = uuid4()
    ticket_id = uuid4()
    now = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    created_at = now - datetime.timedelta(hours=1)

    mock_conn = AsyncMock()
    # LEFT JOIN sla_clocks returns None for due dates and breached flags
    mock_conn.fetch.return_value = [
        {
            "id": ticket_id,
            "status": "open",
            "priority": "medium",
            "sla_profile": "standard",
            "first_response_at": None,
            "resolved_at": None,
            "created_at": created_at,
            "first_response_due": None,
            "resolution_due": None,
            "breached": None,
            "breach_type": None,
            "paused_intervals": None,
        }
    ]

    with (
        patch(
            "nce.vertical_modules.support.sla.scoped_pg_session",
            return_value=_MockContextManager(mock_conn),
        ),
        patch("nce.vertical_modules.support.sla.publish", new_callable=AsyncMock) as mock_pub,
    ):
        result = await do_check_sla_breaches(
            mock_engine,
            {"namespace_id": str(ns_id), "now": now},
        )

        assert result["checked"] == 1
        assert result["breached"] == 0
        assert result["published"] == 0
        mock_pub.assert_not_called()

        # Verify INSERT INTO sla_clocks was called
        insert_calls = [
            c for c in mock_conn.execute.call_args_list if "INSERT INTO sla_clocks" in c[0][0]
        ]
        assert len(insert_calls) == 1
        assert insert_calls[0][0][1] == ticket_id


# ---------------------------------------------------------------------------
# 8. Event Catalogue & Audited Sites Registration
# ---------------------------------------------------------------------------


def test_event_catalogue_and_audited_sites_registration() -> None:
    """Verify TICKET.sla_breached in EVENT_CATALOGUE and watcher site in UNMANAGED_PG_AUDITED_SITES."""
    assert "TICKET.sla_breached" in EVENT_CATALOGUE
    contract = EVENT_CATALOGUE["TICKET.sla_breached"]
    assert contract.node_type == "TICKET"
    assert contract.op == "sla_breached"
    assert "nce/vertical_modules/support/sla.py" in contract.declared_producers
    assert contract.status == "UNCONSUMED"

    assert "cron.support_sla_watcher.namespace_scan" in UNMANAGED_PG_AUDITED_SITES


# ---------------------------------------------------------------------------
# 9. Cron Tick Execution & Distributed Lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_support_sla_watcher_tick_runs_with_lock() -> None:
    """_support_sla_watcher_tick acquires distributed lock and scans namespaces."""
    from nce.cron import _support_sla_watcher_tick

    ns_id = uuid4()
    pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"id": ns_id}]

    with (
        patch("nce.cron.acquire_cron_lock", return_value=MagicMock()) as mock_acquire,
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock) as mock_release,
        patch(
            "nce.cron.unmanaged_pg_connection",
            return_value=_MockContextManager(mock_conn),
        ),
        patch(
            "nce.vertical_modules.support.sla.do_check_sla_breaches",
            new_callable=AsyncMock,
            return_value={
                "checked": 2,
                "breached": 1,
                "published": 1,
                "breaches": [
                    {
                        "ticket_id": str(uuid4()),
                        "breach_type": "first_response",
                        "priority": "critical",
                        "newly_breached": True,
                    }
                ],
            },
        ) as mock_check,
        patch("nce.cron._dispatch_throttled_alert", new_callable=AsyncMock) as mock_alert,
    ):
        await _support_sla_watcher_tick(pool)

        mock_acquire.assert_awaited_once_with("support_sla_watcher", 360)
        mock_check.assert_awaited_once()
        mock_alert.assert_awaited_once()
        mock_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_cron_support_sla_watcher_tick_skips_when_lock_held() -> None:
    """_support_sla_watcher_tick skips cleanly if lock is held by another instance."""
    from nce.cron import _support_sla_watcher_tick

    pool = MagicMock()
    with (
        patch("nce.cron.acquire_cron_lock", return_value=None),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock) as mock_release,
        patch("nce.cron.unmanaged_pg_connection") as mock_conn,
    ):
        await _support_sla_watcher_tick(pool)
        mock_conn.assert_not_called()
        mock_release.assert_not_called()
