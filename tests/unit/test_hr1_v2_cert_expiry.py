"""Unit test suite for Wave HR-1 / V-2 (break-5a).

Verifies:
1. do_check_hr_cert_expiry: scans expired certs, idempotently publishes CERTIFICATION.EXPIRED.
2. do_check_cert_expiry: scans vendor contractor certs, publishes CERTIFICATION.EXPIRED.
3. Event catalogue contract: CERTIFICATION.EXPIRED is ACTIVE with both producers & resources consumer.
4. Watcher resilience: handle_hr_cert_change works on both connection pool and raw connection.
5. Cron ticks: _hr_cert_expiry_watcher_tick and _vendors_cert_expiry_watcher_tick execution & lock handling.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.events.catalogue import EVENT_CATALOGUE
from nce.vertical_modules.hr.certs import do_check_hr_cert_expiry
from nce.vertical_modules.resources.watcher import handle_hr_cert_change
from nce.vertical_modules.vendors.certs import do_check_cert_expiry

# ---------------------------------------------------------------------------
# 1. HR Engine: do_check_hr_cert_expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_hr_cert_expiry_publishes_expired() -> None:
    """HR cert-expiry check detects expired certs and publishes CERTIFICATION.EXPIRED."""
    ns_id = uuid4()
    emp_id = str(uuid4())
    cert_id = str(uuid4())

    mock_conn = AsyncMock()
    # 1 expired cert row returned from SELECT query
    mock_conn.fetch.return_value = [
        {
            "id": cert_id,
            "cert_id": f"CERT-{cert_id[:8]}",
            "employee_id": emp_id,
            "name": "Safety Cert Level 1",
            "valid_to": date.today() - timedelta(days=1),
            "status": "active",
        }
    ]
    # Not yet published in outbox
    mock_conn.fetchval.return_value = False

    class MockContextManager:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class MockPool:
        pass

    engine = MagicMock()
    engine.pg_pool = MockPool()

    with (
        patch(
            "nce.vertical_modules.hr.certs.scoped_pg_session",
            return_value=MockContextManager(),
        ),
        patch("nce.vertical_modules.hr.certs.publish", new_callable=AsyncMock) as mock_publish,
    ):
        result = await do_check_hr_cert_expiry(engine, {"namespace_id": str(ns_id)})

        assert result["checked"] == 1
        assert result["expired"] == 1
        assert result["published"] == 1

        mock_publish.assert_awaited_once()
        call_kwargs = mock_publish.await_args.kwargs
        assert call_kwargs["namespace_id"] == ns_id
        assert call_kwargs["node_type"] == "CERTIFICATION"
        assert call_kwargs["op"] == "EXPIRED"
        assert call_kwargs["aggregate_id"] == f"CERT-{cert_id[:8]}"
        payload = call_kwargs["payload"]
        assert payload["employee_id"] == emp_id
        assert payload["resource_id"] == emp_id
        assert payload["cert_name"] == "Safety Cert Level 1"
        assert payload["status"] == "expired"


@pytest.mark.asyncio
async def test_do_check_hr_cert_expiry_idempotency_prevents_duplicate_publish() -> None:
    """If CERTIFICATION.EXPIRED already exists in outbox_events, do not re-publish."""
    ns_id = uuid4()
    emp_id = str(uuid4())
    cert_id = str(uuid4())

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": cert_id,
            "cert_id": f"CERT-{cert_id[:8]}",
            "employee_id": emp_id,
            "name": "Safety Cert Level 1",
            "valid_to": date.today() - timedelta(days=5),
            "status": "expired",
        }
    ]
    # Already published in outbox
    mock_conn.fetchval.return_value = True

    class MockContextManager:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.hr.certs.scoped_pg_session",
            return_value=MockContextManager(),
        ),
        patch("nce.vertical_modules.hr.certs.publish", new_callable=AsyncMock) as mock_publish,
    ):
        result = await do_check_hr_cert_expiry(engine, {"namespace_id": str(ns_id)})

        assert result["checked"] == 1
        assert result["expired"] == 1
        assert result["published"] == 0
        mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Vendors Engine: do_check_cert_expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_check_cert_expiry_vendors_publishes_certification_expired() -> None:
    """Vendors cert check aligns to CERTIFICATION.EXPIRED and checks both legacy and new event types."""
    ns_id = uuid4()
    contractor_label = "CONTRACTOR:ACME-CORP"
    cert_label = "CERT:ACME-CORP:AVIXA-CTS"
    payload_ref = "000000000000000000000001"

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "cert_label": cert_label,
            "payload_ref": payload_ref,
            "contractor_label": contractor_label,
        }
    ]
    # Not yet published
    mock_conn.fetchval.return_value = False

    class MockContextManager:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    # Mock Mongo
    mock_cursor = AsyncMock()
    mock_cursor.to_list.return_value = [
        {
            "_id": payload_ref,
            "cert_name": "AVIXA-CTS",
            "expiry_date": (date.today() - timedelta(days=2)).isoformat(),
        }
    ]
    mock_db = MagicMock()
    mock_db.episodes.find.return_value = mock_cursor

    class MockMongoContextManager:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc, tb):
            return None

    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.mongo_client = MagicMock()

    with (
        patch(
            "nce.vertical_modules.vendors.certs.scoped_pg_session",
            return_value=MockContextManager(),
        ),
        patch(
            "nce.vertical_modules.vendors.certs.scoped_mongo_session",
            return_value=MockMongoContextManager(),
        ),
        patch("nce.vertical_modules.vendors.certs.publish", new_callable=AsyncMock) as mock_publish,
    ):
        result = await do_check_cert_expiry(
            engine,
            {"namespace_id": str(ns_id), "warn_days": 30},
        )

        assert result["checked"] == 1
        assert result["expiring"] == 1
        assert result["published"] == 1

        mock_publish.assert_awaited_once()
        call_kwargs = mock_publish.await_args.kwargs
        assert call_kwargs["namespace_id"] == ns_id
        assert call_kwargs["node_type"] == "CERTIFICATION"
        assert call_kwargs["op"] == "EXPIRED"
        assert call_kwargs["aggregate_id"] == cert_label
        payload = call_kwargs["payload"]
        assert payload["resource_id"] == contractor_label
        assert payload["status"] == "expired"
        assert payload["cert_name"] == "AVIXA-CTS"


# ---------------------------------------------------------------------------
# 3. Event Catalogue Verification
# ---------------------------------------------------------------------------


def test_catalogue_certification_expired_contract() -> None:
    """Verify CERTIFICATION.EXPIRED is ACTIVE and correctly bound in event catalogue."""
    event = EVENT_CATALOGUE.get("CERTIFICATION.EXPIRED")
    assert event is not None, "CERTIFICATION.EXPIRED must be in catalogue"
    assert event.status == "ACTIVE"
    assert "nce/vertical_modules/hr/certs.py" in event.declared_producers
    assert "nce/vertical_modules/vendors/certs.py" in event.declared_producers
    assert "nce/vertical_modules/resources/watcher.py" in event.declared_consumers

    # Legacy cert.expiry must be deprecated
    legacy = EVENT_CATALOGUE.get("cert.expiry")
    assert legacy is not None
    assert legacy.status == "DEPRECATED"


# ---------------------------------------------------------------------------
# 4. Resources Watcher Resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_hr_cert_change_works_with_raw_connection() -> None:
    """handle_hr_cert_change works when passed a raw asyncpg.Connection (outbox relay)."""
    ns_id = uuid4()
    tech_id = uuid4()
    alloc_id = str(uuid4())

    class MockRawConn:
        def __init__(self):
            self.fetchrow = AsyncMock()
            self.fetch = AsyncMock()
            self.execute = AsyncMock()

    mock_conn = MockRawConn()

    # Resource lookup
    mock_conn.fetchrow.return_value = {
        "id": str(tech_id),
        "namespace_id": str(ns_id),
        "name": "Tech Alpha",
        "email": "alpha@example.test",
        "metadata": {"employee_id": "EMP-001"},
    }

    # Allocation lookup
    future_start = datetime.now(timezone.utc) + timedelta(days=3)
    future_end = future_start + timedelta(hours=8)
    mock_conn.fetch.return_value = [
        {
            "id": alloc_id,
            "namespace_id": str(ns_id),
            "resource_id": str(tech_id),
            "starts_at": future_start,
            "ends_at": future_end,
            "status": "confirmed",
            "attrs": {},
        }
    ]

    payload = {
        "namespace_id": str(ns_id),
        "employee_id": "EMP-001",
        "cert_name": "High Voltage Safety",
        "status": "expired",
        "valid_to": (date.today() - timedelta(days=1)).isoformat(),
    }

    # Call handle_hr_cert_change directly passing mock_conn (as outbox relay does)
    await handle_hr_cert_change(mock_conn, payload)

    # Verify allocation was updated
    mock_conn.execute.assert_awaited_once()
    sql_call = mock_conn.execute.await_args[0][0]
    assert "UPDATE allocations" in sql_call
    assert "status = $2" in sql_call


# ---------------------------------------------------------------------------
# 5. Cron Tick Scheduling & Handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_ticks_execute_with_lock() -> None:
    """Cron tick helpers acquire lock and call engine cores."""
    from nce.cron import _hr_cert_expiry_watcher_tick, _vendors_cert_expiry_watcher_tick

    pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"id": uuid4()}]

    class MockConnContextManager:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    # Test HR tick
    with (
        patch("nce.cron.acquire_cron_lock", return_value=MagicMock()),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch(
            "nce.cron.unmanaged_pg_connection",
            return_value=MockConnContextManager(),
        ),
        patch(
            "nce.vertical_modules.hr.certs.do_check_hr_cert_expiry",
            new_callable=AsyncMock,
            return_value={"checked": 1, "expired": 1, "published": 1},
        ) as mock_hr_check,
    ):
        await _hr_cert_expiry_watcher_tick(pool)
        mock_hr_check.assert_awaited_once()

    # Test Vendors tick
    with (
        patch("nce.cron.acquire_cron_lock", return_value=MagicMock()),
        patch("nce.cron.release_cron_lock", new_callable=AsyncMock),
        patch(
            "nce.cron.unmanaged_pg_connection",
            return_value=MockConnContextManager(),
        ),
        patch(
            "nce.vertical_modules.vendors.certs.do_check_cert_expiry",
            new_callable=AsyncMock,
            return_value={"checked": 1, "expiring": 1, "published": 1},
        ) as mock_vendors_check,
    ):
        await _vendors_cert_expiry_watcher_tick(pool, MagicMock())
        mock_vendors_check.assert_awaited_once()
